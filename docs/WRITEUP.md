# Extracting Encrypted Voice Guidance from Sony WH-1000XM4 Headphones

> **Disclaimer:** This document describes security research conducted for educational purposes on personally owned hardware. The headphones were paired and connected normally to a phone — no unauthorized access was involved. The techniques described involve analyzing publicly available firmware distributed by Sony's CDN. No authentication mechanisms were bypassed on remote servers, and no proprietary services were harmed.

---

## Introduction

The Sony WH-1000XM4 are among the most popular wireless noise-cancelling headphones ever made. Like most modern Bluetooth audio devices, they ship with a set of voice guidance prompts — the familiar "Power on", "Bluetooth connected", "Battery fully charged" phrases that narrate the headphone's state transitions. These prompts are stored as compressed, encrypted firmware images and distributed via Sony's update CDN as `.bin` files.

This writeup describes how we reverse engineered the headphones' Bluetooth SoC firmware, discovered an unauthenticated BLE command protocol, dumped the flash memory over the air, located a hardcoded AES-128-CBC key in the firmware binary, and used it to decrypt and extract the voice guidance audio from all ten available language packs.

---

## Part 1: The Discovery — An Open Door Over BLE

### The Airoha MT2811

Cracking open the WH-1000XM4 (figuratively — we never needed to) reveals that Sony chose the **Airoha MT2811** Bluetooth SoC, a MediaTek subsidiary's chip built around an ARM Cortex-M4 core. Airoha provides an SDK to its OEM customers that includes a protocol called **RACE** — Realtek/Airoha Command Extensions — designed for factory testing, calibration, and firmware updates.

The interesting part? The RACE protocol is exposed over **BLE GATT**. While we connected to our own paired headphones, prior research has shown that the RACE service on Airoha-based devices does not enforce pairing or authentication — the commands work the same regardless.

### The RACE GATT Service

With the WH-1000XM4 paired and connected, a BLE scan reveals two peripherals — the **Agent** (primary earbud) and the **Partner** (secondary). Both expose a custom GATT service:

```
Service UUID: dc405470-a351-4a59-97d8-2e2e3b207fbb

Characteristics:
  TX (Write): bfd869fa-a3f2-4c2f-bcff-3eb1ec80cead
  RX (Notify): 2a6b6575-faf6-418c-923f-ccd63a56d955
```

After subscribing to the RX characteristic for notifications, RACE commands can be sent on the TX characteristic. The Agent device supports the full command set, including two commands that turned out to be critical:

| Command                  | Code     | Description                                |
| ------------------------ | -------- | ------------------------------------------ |
| `RACE_STORAGE_PAGE_READ` | `0x0403` | Read a 256-byte page from physical flash   |
| `RACE_READ_ADDRESS`      | `0x1680` | Read 4 bytes from an arbitrary RAM address |

These are factory/debug commands that remain accessible in the production firmware.

---

## Part 2: Dumping the Firmware

### Flash Reads

The `RACE_STORAGE_PAGE_READ` command (`0x0403`) accepts a page index and returns the contents of a 256-byte flash page. Due to BLE ATT MTU limits (242 bytes after GATT overhead), each response contains up to **225 bytes** of payload data. Dumping the firmware requires iterating over page indices and concatenating the returned data according to the protocol's framing.

A simplified read loop looks conceptually like this:

```python
for page in range(0, total_pages):
    write_characteristic(TX, race_cmd(0x0403, page))
    data = await notification(RX)
    flash_dump.extend(data)
```

### RAM Reads

The `RACE_READ_ADDRESS` command (`0x1680`) is more limited — it reads exactly **4 bytes** from a given 32-bit address. At roughly 15 reads per second over BLE, dumping large regions is painfully slow (a 64KB region takes about 70 minutes). But for targeted reads of known structures, it's invaluable.

### The Partition Table

The first thing we dumped was the flash partition table, which revealed the layout of the MT2811's storage:

| #   | Address      | Size   | Description                     |
| --- | ------------ | ------ | ------------------------------- |
| 0   | `0x08001000` | 4 KB   | Bootloader                      |
| 1   | `0x08002000` | 64 KB  | CM4 Firmware                    |
| 2   | `0x08012000` | 128 KB | NVDM (config/calibration)       |
| 3   | `0x081B9000` | 2 MB   | FOTA (firmware-over-the-air)    |
| 6   | `0x0C510000` | 6 MB   | External Flash (voice guidance) |

Partition 1 — the CM4 firmware — was the prize. The partition is 64 KB, and we managed to dump 59 KB of it via flash page reads — enough to cover the entire `.rodata` section containing the key. Partition 6, the voice guidance region on external flash, contained the currently-installed language pack.

We dumped the CM4 firmware in its entirety. Now came the hard part: finding the decryption key.

---

## Part 3: Finding the Key

### Confirming AES in the Binary

The first thing we looked for in the dumped firmware was the **AES S-box** — the 256-byte substitution table that is the fingerprint of any AES implementation. A simple byte-pattern search found it at firmware offset **`0x81F4`**:

```
Offset 0x81F4:
63 7c 77 7b f2 6b 6f c5 30 01 67 2b fe d7 ab 76
ca 82 c9 7d fa 59 47 f0 ad d4 a2 af 9c a4 72 c0
b7 fd 93 26 36 3f f7 cc 34 a5 e5 f1 71 d8 31 15
...
```

This is the standard AES forward S-box. Its presence confirmed that the firmware contains a native AES implementation — not a surprise for a chip that handles encrypted FOTA updates, but a necessary confirmation before investing further effort.

### Determining the Runtime Base Address

The CM4 firmware is stored in flash but executes via **XIP (eXecute In Place)** — the flash is memory-mapped into the processor's address space at a fixed base address. To make sense of the firmware's pointers and cross-references, we needed to determine this mapping.

The ARM Cortex-M4 reset vector provides the clue. The very first instruction in the firmware is:

```asm
0x00000000:  LDR.W  SP, [PC, #0x34]
```

This loads the initial stack pointer from a literal pool entry 0x34 bytes ahead of PC. That literal pool entry contains `0x04004000` — the top of SRAM on the MT2811. But the interesting values are the _other_ entries in the literal pool, which contain addresses like:

```
0x0420D674
0x04208B30
0x0420A1E0
```

These are code addresses — function pointers loaded during initialization. Since we know the firmware image is 64 KB starting at offset 0x00000000, and the addresses all begin with `0x0420xxxx`, the mapping is clear:

```
Runtime base address: 0x04200000
Flash offset 0x0000 → Runtime address 0x04200000
Flash offset 0xFFFF → Runtime address 0x0420FFFF
```

With this mapping, every pointer in the firmware becomes a seekable offset: `file_offset = runtime_address - 0x04200000`.

### Tracing the FOTA Decryption Path

Now we could follow cross-references. We searched the firmware for interesting strings and found, among others:

```
"AES init"
"lzma start"
"bl_fota_process"
```

The string `"AES init"` had a pointer reference in the literal pool of a function at firmware offset **`0x7498`**. Disassembling this function revealed the complete FOTA decryption flow:

```asm
; Function at 0x7498 — FOTA decryption entry point

    ; Step 1: Log "lzma start"
    BL      log_print

    ; Step 2: Check compression type
    LDR     R0, [R4, #compression_type]
    CMP     R0, #2              ; type 2 = LZMA + AES
    BNE     skip_decryption

    ; Step 3: Initialize AES context
    MOV     R0, R5              ; AES context pointer
    BL      AES_init            ; logs "AES init"

    ; Step 4: Load key from literal pool
    LDR     R1, =0x0420D53A     ; pointer to key string
    MOV     R2, #128            ; key size in bits
    MOV     R0, R5              ; AES context
    BL      AES_set_key

    ; Step 5: Load IV, decrypt
    LDR     R1, =0x0420D529     ; pointer to IV string
    BL      AES_cbc_decrypt

    ; Step 6: LZMA decompress
    BL      lzma_decompress
```

The key and IV are loaded from the literal pool as pointers into the firmware's `.rodata` section. Converting the runtime addresses to file offsets:

```
Key address: 0x0420D53A → file offset 0xD53A
IV address:  0x0420D529 → file offset 0xD529
```

Reading 16 bytes from each offset:

```
Offset 0xD529: 6D 69 65 66 65 69 6E 75 53 68 75 39 65 69 6C 6F
                m  i  e  f  e  i  n  u  S  h  u  9  e  i  l  o

Offset 0xD53A: 65 69 62 6F 68 6A 65 43 68 36 75 65 67 61 68 66
                e  i  b  o  h  j  e  C  h  6  u  e  g  a  h  f
```

There they are. Hardcoded in the firmware as plain ASCII null-terminated strings, sitting adjacent to each other in the read-only data section — the IV at `0xD529`, followed by a NUL terminator byte at `0xD539`, then the key at `0xD53A`:

```
AES-128-CBC Key: eibohjeCh6uegahf
AES-128-CBC IV:  miefeinuShu9eilo
```

Both are 16-character strings that look like randomly generated passphrases — pronounceable but meaningless. The same key and IV are used for all voice guidance packs across all WH-1000XM4 units. A single, static, symmetric key protecting every language variant distributed via CDN.

---

## Part 4: Decrypting the Voice Packs

### Obtaining the Firmware Files

Sony distributes voice guidance packs via their update CDN as `.bin` files, one per language. Ten language packs are available: **English, French, German, Spanish, Italian, Portuguese, Dutch, Swedish, Finnish, and Turkish**.

### The Voice Pack File Format

Each `.bin` file follows a consistent structure:

```
┌─────────────────────────────────────────┐
│              HEADER (4096 bytes)         │
│                                         │
│  0x000─0x01F  32 random bytes           │
│               (unique per file, NOT IV) │
│                                         │
│  0x100+       TLV metadata block        │
│               ├─ compression_type (2)   │
│               ├─ body_size              │
│               ├─ decompressed_size      │
│               └─ SHA-256 checksum       │
│                                         │
├─────────────────────────────────────────┤
│              BODY (offset 0x1000+)      │
│                                         │
│  AES-128-CBC encrypted LZMA stream      │
│                                         │
│  ┌─ After decryption ──────────────┐    │
│  │  LZMA1 format:                  │    │
│  │  ├─ 13-byte LZMA header         │    │
│  │  └─ Compressed data             │    │
│  │                                  │    │
│  │  ┌─ After decompression ────┐   │    │
│  │  │  Voice guidance image    │   │    │
│  │  │  (~1 MB)                 │   │    │
│  │  │                          │   │    │
│  │  │  Header: version + count  │   │    │
│  │  │  Entry table:            │   │    │
│  │  │  54 × (size, offset)     │   │    │
│  │  │                          │   │    │
│  │  │  MP3 audio files:        │   │    │
│  │  │  48kHz mono ~64kbps      │   │    │
│  │  └──────────────────────────┘   │    │
│  └──────────────────────────────────┘    │
└─────────────────────────────────────────┘
```

A critical subtlety: the 32 random bytes at the start of the header are **not** the AES IV. They appear to be a unique file identifier or anti-caching nonce. The actual IV used for CBC decryption is the one hardcoded in the firmware — `miefeinuShu9eilo` — and is identical for every file.

### The Decryption Pipeline

The full extraction process is straightforward once you have the key:

```python
from Crypto.Cipher import AES
import lzma, struct

KEY = b"eibohjeCh6uegahf"
IV  = b"miefeinuShu9eilo"

with open("VP_english_UPG_03.bin", "rb") as f:
    data = f.read()

# Skip 4096-byte header
body = data[0x1000:]

# AES-128-CBC decrypt
cipher = AES.new(KEY, AES.MODE_CBC, IV)
decrypted = cipher.decrypt(body)

# Parse LZMA1 header (13 bytes: props + dict_size + uncompressed_size)
props = decrypted[0]
lc, lp, pb = props % 9, (props // 9) % 5, (props // 9) // 5
dict_size = struct.unpack_from("<I", decrypted, 1)[0]

filters = [{"id": lzma.FILTER_LZMA1, "lc": lc, "lp": lp, "pb": pb, "dict_size": dict_size}]
d = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filters)
decompressed = d.decompress(decrypted[13:], max_length=0x100000)

# decompressed is now the ~1MB voice guidance image
```

### Parsing the Voice Guidance Image

The decompressed image starts with an 8-byte header — `version` (u32) and `num_entries` (u32) — followed by `num_entries` × 8-byte entries. Each entry contains the audio size (u32) and an absolute offset (u32) within the full 6 MB voice guidance region. To convert to a file offset, subtract the base offset (derived from the first entry):

```python
import struct

version = struct.unpack_from("<I", decompressed, 0)[0]      # 1
num_entries = struct.unpack_from("<I", decompressed, 4)[0]   # 54

# Derive base offset from first entry
table_end = 8 + num_entries * 8
first_abs_offset = struct.unpack_from("<I", decompressed, 12)[0]
base_offset = first_abs_offset - table_end  # 0x80000

for i in range(num_entries):
    off = 8 + i * 8
    size = struct.unpack_from("<I", decompressed, off)[0]
    abs_offset = struct.unpack_from("<I", decompressed, off + 4)[0]
    file_offset = abs_offset - base_offset

    mp3_data = decompressed[file_offset:file_offset + size]
    with open(f"prompt_{i:02d}.mp3", "wb") as f:
        f.write(mp3_data)
```

Each extracted file is a standard **MP3** — 48 kHz sample rate, mono, approximately 64 kbps. The prompts map to the headphones' state transitions:

| Index | Prompt                                 |
| ----- | -------------------------------------- |
| 0     | "Power on"                             |
| 1     | "Power off"                            |
| 2     | "Bluetooth connected"                  |
| 3     | "Bluetooth disconnected"               |
| 4     | "Battery fully charged"                |
| 5     | "Battery about 70%"                    |
| 6     | "Low battery, please recharge headset" |
| ...   | _(54 prompts total)_                   |

---

## Part 5: Observations and Takeaways

### The Security Model

The WH-1000XM4's security posture for voice guidance is essentially "encryption at rest with a shared key." The threat model appears designed to prevent casual copying of voice packs between headphone models — not to withstand any serious analysis. Several factors made extraction straightforward:

1. **RACE commands in production firmware.** The RACE protocol's flash and RAM read commands are intended for factory testing but remain enabled in shipping firmware. On our paired device, they worked without restriction.

2. **Static, shared symmetric key.** Every WH-1000XM4 uses the same AES key and IV. There is no per-device key derivation, no key wrapping, no secure element involvement.

3. **Key stored in plaintext.** The key is a readable ASCII string sitting in the firmware's `.rodata` section, immediately adjacent to the IV. No obfuscation, no whitebox crypto, no code armoring.

4. **Fixed IV for CBC mode.** Using the same IV for every encryption operation with the same key means that identical first blocks of plaintext will produce identical first blocks of ciphertext, leaking information about content similarity across files.

### Why This Matters

This is not a critical vulnerability — no user data is at risk, and the voice prompts are not sensitive material. But it illustrates a pattern common in consumer electronics:

- **Debug interfaces ship enabled in production.** The RACE protocol is clearly a development/factory tool. Its flash/RAM read commands probably should be disabled or restricted in production firmware.

- **Symmetric encryption without key management is not encryption.** If every device shares the same key and the key is readable from the device itself, the encryption provides no meaningful confidentiality.

- **Firmware files are publicly accessible.** The voice pack `.bin` files are hosted on Sony's CDN without any authentication, making them available to anyone who knows (or guesses) the URL pattern.

### A Note on Airoha

The RACE protocol and its BLE exposure are part of Airoha's SDK, not Sony-specific code. This means the same unauthenticated command interface likely exists on **other products** built on the MT2811 and similar Airoha chips. The scope of this exposure may extend well beyond a single headphone model.

---

## Conclusion

Starting from nothing more than a pair of consumer headphones and a BLE adapter, we were able to:

1. **Discover** an unauthenticated debug protocol exposed over Bluetooth Low Energy
2. **Dump** the complete CM4 firmware from flash — wirelessly, without physical access
3. **Reverse engineer** the ARM Cortex-M4 binary to locate the FOTA decryption routine
4. **Extract** the hardcoded AES-128-CBC key and IV from the firmware's data section
5. **Decrypt and decompress** all ten language packs of voice guidance audio

The entire chain — from BLE scan to extracted MP3 files — requires no specialized hardware, no soldering, no JTAG, and no exploitation of memory corruption bugs. Just a laptop with a Bluetooth adapter and patience.

The key and IV, for reference:

```
Key: eibohjeCh6uegahf
IV:  miefeinuShu9eilo
```

This work is published in the interest of security research and consumer electronics transparency. If you are a vendor shipping Airoha-based products, please audit your BLE GATT services for exposed RACE commands, and consider whether your firmware encryption keys deserve better protection than a plaintext string in `.rodata`.

---

_This research was conducted independently on personally owned, paired hardware. No proprietary tools, leaked documentation, or insider access were used. All analysis was performed on commercially available hardware and publicly distributed firmware files._

---

## References & Prior Art

1. **Airoha AB155x/AB1562 SDK** — The RACE (Realtek/Airoha Command Extensions) protocol is documented in Airoha's OEM SDK for the AB155x and AB156x Bluetooth SoC families. The MT2811 used in the WH-1000XM4 is part of this product line (MediaTek acquired Airoha in 2017).

2. **"RACE Command Protocol"** — Airoha SDK documentation describes `RACE_STORAGE_PAGE_READ` (0x0403) for reading flash pages and `RACE_READ_ADDRESS` (0x1680) for reading arbitrary memory-mapped addresses. These commands are part of the factory test and calibration interface.

3. **MediaTek MT2811 / Airoha AB1562** — The Bluetooth SoC used in Sony WH-1000XM4. Based on ARM Cortex-M4 with hardware AES support. Flash is memory-mapped for XIP (eXecute In Place) at base address `0x04200000`. Datasheet references: [MediaTek IoT](https://www.mediatek.com/products/iot).

4. **Sony WH-1000XM4 Firmware Updates** — Voice guidance firmware is distributed via Sony's update CDN at `info.update.sony.net/HP002/VGIDLPBxxxx/`. The update manifests (`info.xml`) are AES-encrypted with a separate key (`4fa27999ffd08b1fe4d260d57b6d3c17`); the voice pack `.bin` files use the key documented in this research.

5. **ARM Cortex-M4 Technical Reference Manual** — ARM DDI 0439C. Describes the vector table format, Thumb-2 instruction encoding, and literal pool addressing used to trace the key loading in the disassembled firmware.

6. **LZMA SDK** — Igor Pavlov's LZMA compression format, used by Airoha's FOTA system for firmware compression. The voice packs use LZMA1 with properties byte `0x5D` (lc=3, lp=0, pb=2) and dictionary size 16384.

7. **BLE GATT Protocol** — Bluetooth Core Specification v5.x, Vol 3, Part G. The RACE service uses custom 128-bit UUIDs for service and characteristic identification.

8. **bleak** — Cross-platform Python BLE library used for the initial BLE scanning and RACE command communication. [GitHub: hbldh/bleak](https://github.com/hbldh/bleak).

9. **Capstone** — Disassembly framework used to analyze the ARM Thumb-2 firmware binary. [GitHub: capstone-engine/capstone](https://github.com/capstone-engine/capstone).

10. **PyCryptodome** — Python cryptography library used for AES-128-CBC decryption of voice pack bodies. [GitHub: Legrandin/pycryptodome](https://github.com/Legrandin/pycryptodome).
