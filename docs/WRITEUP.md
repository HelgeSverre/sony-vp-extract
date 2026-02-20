# Extracting Encrypted Voice Guidance from Sony WH-1000XM4 Headphones

> **Note:** Everything described here was done on my own paired headphones. The voice pack files are publicly accessible on Sony's CDN; the firmware was dumped from the headphones themselves over BLE. No authentication mechanisms were bypassed on remote servers.

---

## Introduction

The Sony WH-1000XM4, like most modern Bluetooth audio devices, ships with a set of voice guidance prompts — "Power on", "Bluetooth connected", "Battery fully charged" and so on. These prompts are stored as compressed, encrypted voice pack files and distributed via Sony's update CDN as `.bin` files.

I wanted to see if I could extract these prompts — partly out of curiosity about how the update system works, and partly as an experiment in using AI-assisted tooling for hardware reverse engineering. The bulk of the analysis, scripting, and binary exploration described here was done with [Ampcode](https://ampcode.com), an AI coding agent, with me directing the investigation and providing the hardware. Prior research on the Airoha RACE protocol and existing APK teardowns provided the initial footholds.

This writeup describes the process: dumping the headphones' Bluetooth SoC firmware over BLE, locating an AES-128-CBC key in the binary, and using it to decrypt and extract the voice guidance audio from all ten available language packs.

---

## Part 1: The RACE Protocol

### The Airoha MT2811

The WH-1000XM4 uses the **Airoha MT2811** Bluetooth SoC, a MediaTek subsidiary's chip built around an ARM Cortex-M4 core. Airoha provides an SDK to its OEM customers that includes a protocol called **RACE** — Realtek/Airoha Command Extensions — designed for factory testing, calibration, and firmware updates.

The RACE protocol is exposed over **BLE GATT**. Prior research on other Airoha-based devices has documented this interface extensively. On my own paired headphones, the commands were accessible without any additional setup.

### The RACE GATT Service

With the WH-1000XM4 paired and connected, a BLE scan reveals two peripherals — the **Agent** (primary) and the **Partner** (secondary). Both expose a custom GATT service:

```
Service UUID: dc405470-a351-4a59-97d8-2e2e3b207fbb

Characteristics:
  TX (Write): bfd869fa-a3f2-4c2f-bcff-3eb1ec80cead
  RX (Notify): 2a6b6575-faf6-418c-923f-ccd63a56d955
```

After subscribing to the RX characteristic for notifications, RACE commands can be sent on the TX characteristic. The Agent device supports the full command set, including two commands that are relevant here:

| Command                  | Code     | Description                                |
| ------------------------ | -------- | ------------------------------------------ |
| `RACE_STORAGE_PAGE_READ` | `0x0403` | Read a 256-byte page from physical flash   |
| `RACE_READ_ADDRESS`      | `0x1680` | Read 4 bytes from an arbitrary RAM address |

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

The first thing I dumped was the flash partition table, which revealed the layout of the MT2811's storage:

| #   | Address      | Size   | Description                     |
| --- | ------------ | ------ | ------------------------------- |
| 0   | `0x08001000` | 4 KB   | Bootloader                      |
| 1   | `0x08002000` | 64 KB  | CM4 Firmware                    |
| 2   | `0x08012000` | 128 KB | NVDM (config/calibration)       |
| 3   | `0x081B9000` | 2 MB   | FOTA (firmware-over-the-air)    |
| 6   | `0x0C510000` | 6 MB   | External Flash (voice guidance) |

Partition 1 — the CM4 firmware — is the one that matters here. The partition is 64 KB, and I was able to dump 59 KB of it via flash page reads — enough to cover the entire `.rodata` section. Partition 6, the voice guidance region on external flash, contained the currently-installed language pack.

---

## Part 3: Finding the Key

### Confirming AES in the Binary

The first thing to look for in the dumped firmware was the **AES S-box** — the 256-byte substitution table that is the fingerprint of any AES implementation. A simple byte-pattern search found it at firmware offset **`0x81F4`**:

```
Offset 0x81F4:
63 7c 77 7b f2 6b 6f c5 30 01 67 2b fe d7 ab 76
ca 82 c9 7d fa 59 47 f0 ad d4 a2 af 9c a4 72 c0
b7 fd 93 26 36 3f f7 cc 34 a5 e5 f1 71 d8 31 15
...
```

This is the standard AES forward S-box, confirming the firmware contains a native AES implementation.

### Determining the Runtime Base Address

The CM4 firmware is stored in flash but executes via **XIP (eXecute In Place)** — the flash is memory-mapped into the processor's address space at a fixed base address. To make sense of the firmware's pointers and cross-references, this mapping needs to be determined.

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

These are code addresses — function pointers loaded during initialization. Since the firmware image is 64 KB starting at offset 0x00000000, and the addresses all begin with `0x0420xxxx`, the mapping is clear:

```
Runtime base address: 0x04200000
Flash offset 0x0000 → Runtime address 0x04200000
Flash offset 0xFFFF → Runtime address 0x0420FFFF
```

With this mapping, every pointer in the firmware becomes a seekable offset: `file_offset = runtime_address - 0x04200000`.

### Tracing the FOTA Decryption Path

With this mapping, cross-references become traceable. Searching the firmware for interesting strings turned up, among others:

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

Both are plain ASCII null-terminated strings, adjacent in the read-only data section — the IV at `0xD529`, followed by a NUL terminator at `0xD539`, then the key at `0xD53A`:

```
AES-128-CBC Key: eibohjeCh6uegahf
AES-128-CBC IV:  miefeinuShu9eilo
```

Both are 16-character strings that look like randomly generated passphrases. The same key and IV are used for all voice guidance packs across all WH-1000XM4 units.

---

## Part 4: Decrypting the Voice Packs

### Obtaining the Voice Pack Files

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

Each extracted file is a standard **MP3** — 48 kHz sample rate, mono, approximately 64 kbps. The 54 prompts cover state transitions, battery levels, and feature notifications:

| Index | Prompt                                                                                          |
| ----- | ----------------------------------------------------------------------------------------------- |
| 0     | "Power on"                                                                                      |
| 1     | "Power off"                                                                                     |
| 2     | "Bluetooth pairing"                                                                             |
| 3     | "Bluetooth connected"                                                                           |
| 4     | "Bluetooth disconnected"                                                                        |
| 5     | "Please recharge headset. Power off"                                                            |
| 6     | "Noise canceling"                                                                               |
| 7     | "Ambient Sound Control off"                                                                     |
| 8     | "Ambient sound"                                                                                 |
| 9     | "The Google Assistant is not available during update. Please wait a moment until the update completes" |
| 10–14 | _(notification tones)_                                                                          |
| 15    | "Battery fully charged"                                                                         |
| 16    | "Battery about 70%"                                                                             |
| 17    | "Battery about 50%"                                                                             |
| 18    | "Battery about 20%"                                                                             |
| 19–20 | _(notification tones)_                                                                          |
| 21    | "The Google Assistant is not connected"                                                         |
| 22–23 | _(notification tones)_                                                                          |
| 24    | "Either your mobile device isn't connected; or you need to open the Alexa App and try again"    |
| 25–26 | _(notification tones)_                                                                          |
| 27    | _(unknown — short speech, possibly assistant-related)_                                          |
| 28    | "Battery about 90%"                                                                             |
| 29    | "Battery about 80%"                                                                             |
| 30    | "Battery about 60%"                                                                             |
| 31    | "Battery about 40%"                                                                             |
| 32    | "Battery about 30%"                                                                             |
| 33    | "Battery about 10%"                                                                             |
| 34    | "Low battery, please recharge headset"                                                          |
| 35    | "Optimizer start"                                                                               |
| 36    | "Optimizer finished"                                                                            |
| 37    | _(notification tone)_                                                                           |
| 38    | _(unknown — short speech)_                                                                      |
| 39–42 | _(notification tones)_                                                                          |
| 43    | "Speak-to-chat activated"                                                                       |
| 44    | "Speak-to-chat deactivated"                                                                     |
| 45–46 | _(notification tones)_                                                                          |
| 47    | "Bluetooth Device 1 connected"                                                                  |
| 48    | "Bluetooth Device 1 disconnected"                                                               |
| 49    | "Bluetooth Device 2 connected"                                                                  |
| 50    | "Bluetooth Device 2 disconnected"                                                               |
| 51    | "Bluetooth Device 1 replaced"                                                                   |
| 52    | "Bluetooth Device 2 replaced"                                                                   |
| 53    | "Bluetooth 2nd device connected"                                                                |

---

## Part 5: The CDN Manifest Layer

### Update Manifests

Sony's update CDN hosts encrypted manifest files at predictable URLs following the pattern:

```
https://info.update.sony.net/HP002/VGIDLPB0401/info/info.xml
```

These manifests use a **different** cryptographic scheme from the voice packs themselves. While voice packs use AES-128-CBC with a key found in the headphone firmware, the manifests use **AES-128-ECB** with a key extracted from the Sony Sound Connect Android APK:

```
Manifest key: 4fa27999ffd08b1fe4d260d57b6d3c17
Mode:         AES-128-ECB (no IV)
```

### Manifest File Format

The encrypted manifest file has a plaintext header followed by the AES-encrypted body, separated by a double newline:

```
eaid:ENC0003              ← encryption scheme identifier
daid:HAS0003              ← hash scheme identifier
digest:<sha1_hex>         ← SHA-1 hash of the encrypted payload
                          ← (blank line)
<AES-128-ECB encrypted XML body>
```

The `eaid` field identifies the encryption version (`ENC0003`), while `daid` identifies the integrity check method (`HAS0003` — SHA-1).

### Decrypted Manifest Contents

After decryption, the manifest is a standard XML document describing available firmware distributions:

```xml
<InformationFile LastUpdate="2020-03-19T11:41:20Z" Version="1.0">
    <ControlConditions DefaultServiceStatus="open"/>
    <ApplyConditions>
        <ApplyCondition ApplyOrder="1" Force="false">
            <Distributions>
                <Distribution ID="FW"
                    URI="https://info.update.sony.net/.../VP_english_UPG_03.bin"
                    MAC="b754767733623779af2b9f0faf13f07be0c43593"
                    Size="911456"
                    Version="3"/>
            </Distributions>
        </ApplyCondition>
    </ApplyConditions>
</InformationFile>
```

Each `Distribution` entry contains the CDN download URL, a SHA-1 hash (`MAC`) for integrity verification, file size, and version number — everything needed to automate discovery and downloading of voice packs.

### Two-Tier Key Architecture

Sony uses two completely independent encryption keys for their update infrastructure:

| Layer     | Algorithm    | Key Source         | Key                                |
| --------- | ------------ | ------------------ | ---------------------------------- |
| Manifests | AES-128-ECB  | Android APK        | `4fa27999ffd08b1fe4d260d57b6d3c17` |
| Voice packs | AES-128-CBC | Headphone firmware | `eibohjeCh6uegahf` (+ IV)         |

The manifest key protects the update metadata — which files exist, their URLs, and integrity hashes. The voice pack key protects the actual audio content.

The actual headphone firmware updates are not served through this CDN path. They go through an authenticated configuration service (`hpc-cfgdst-ore-prd.pdp.bda.ndmdhs.com`) that requires app-specific headers from the Sony Sound Connect app.

---

## Part 6: How It All Fits Together

To summarize the system as observed:

- The RACE protocol, part of Airoha's SDK, exposes flash and RAM read commands over BLE GATT. These are factory/debug commands that are present in the production firmware.
- The AES-128-CBC key and IV are stored as plaintext ASCII strings in the CM4 firmware's `.rodata` section. The same key and IV are used across all WH-1000XM4 units and all language packs.
- The same IV is reused for every file encrypted with the same key.
- The voice pack `.bin` files are hosted on Sony's CDN without authentication — the URLs follow a predictable pattern.
- The CDN manifests use a separate AES-128-ECB key, embedded in the Android companion app.
- Actual firmware updates (as opposed to voice packs) are distributed through a different, authenticated service.

None of this is particularly unusual for consumer electronics. Voice prompts are low-value assets — the encryption likely exists to manage distribution and compatibility across headphone models rather than to protect the audio content itself. The RACE protocol is a standard part of Airoha's chipset SDK and is not Sony-specific.

---

## Conclusion

Starting from a pair of headphones, a BLE adapter, and an AI coding agent, the process went:

1. Connected to the headphones' RACE protocol over BLE (documented in prior research on Airoha devices)
2. Dumped the CM4 firmware from flash — wirelessly, no physical access needed
3. Located the AES S-box and traced the FOTA decryption routine in the ARM Cortex-M4 binary
4. Extracted the AES-128-CBC key and IV from the firmware's data section
5. Decrypted the CDN manifests using a second key found in the Android companion app
6. Decrypted and decompressed all ten language packs into individual MP3 files

No specialized hardware, no soldering, no JTAG. The heavy lifting — disassembly, binary analysis, scripting — was done by Ampcode with me steering. What would traditionally take days of manual work in Ghidra was done in an afternoon through iterative prompting.

The key and IV, for reference:

```
Key: eibohjeCh6uegahf
IV:  miefeinuShu9eilo
```

---

## References & Prior Art

1. **Airoha AB155x/AB1562 SDK** — The RACE (Realtek/Airoha Command Extensions) protocol is documented in Airoha's OEM SDK for the AB155x and AB156x Bluetooth SoC families. The MT2811 used in the WH-1000XM4 is part of this product line (MediaTek acquired Airoha in 2017).

2. **"RACE Command Protocol"** — Airoha SDK documentation describes `RACE_STORAGE_PAGE_READ` (0x0403) for reading flash pages and `RACE_READ_ADDRESS` (0x1680) for reading arbitrary memory-mapped addresses. These commands are part of the factory test and calibration interface.

3. **MediaTek MT2811 / Airoha AB1562** — The Bluetooth SoC used in Sony WH-1000XM4. Based on ARM Cortex-M4 with hardware AES support. Flash is memory-mapped for XIP (eXecute In Place) at base address `0x04200000`. Datasheet references: [MediaTek IoT](https://www.mediatek.com/products/iot).

4. **Sony WH-1000XM4 Voice Packs** — Voice guidance packs are distributed via Sony's update CDN at `info.update.sony.net/HP002/VGIDLPBxxxx/`. The update manifests (`info.xml`) are AES-encrypted with a separate key (`4fa27999ffd08b1fe4d260d57b6d3c17`); the voice pack `.bin` files use the key documented in this research.

5. **ARM Cortex-M4 Technical Reference Manual** — ARM DDI 0439C. Describes the vector table format, Thumb-2 instruction encoding, and literal pool addressing used to trace the key loading in the disassembled firmware.

6. **LZMA SDK** — Igor Pavlov's LZMA compression format, used by Airoha's FOTA system for firmware compression. The voice packs use LZMA1 with properties byte `0x5D` (lc=3, lp=0, pb=2) and dictionary size 16384.

7. **BLE GATT Protocol** — Bluetooth Core Specification v5.x, Vol 3, Part G. The RACE service uses custom 128-bit UUIDs for service and characteristic identification.

8. **bleak** — Cross-platform Python BLE library used for the initial BLE scanning and RACE command communication. [GitHub: hbldh/bleak](https://github.com/hbldh/bleak).

9. **Capstone** — Disassembly framework used to analyze the ARM Thumb-2 firmware binary. [GitHub: capstone-engine/capstone](https://github.com/capstone-engine/capstone).

10. **PyCryptodome** — Python cryptography library used for AES-128-CBC decryption of voice pack bodies. [GitHub: Legrandin/pycryptodome](https://github.com/Legrandin/pycryptodome).

11. **ERNW — "Bluetooth Headphone Jacking"** — Full disclosure of Airoha RACE vulnerabilities (CVE-2025-20700, CVE-2025-20701, CVE-2025-20702) by ERNW, presented at 39C3 in December 2025. Documented the RACE protocol's BLE/Classic exposure, flash/RAM read commands, and link key extraction across 29 affected devices including the WH-1000XM4. [Blog post](https://insinuator.net/2025/12/bluetooth-headphone-jacking-full-disclosure-of-airoha-race-vulnerabilities/), [RACE Toolkit](https://github.com/auracast-research/race-toolkit).

12. **airoha-firmware-parser** — Tool for unpacking Airoha FOTA firmware images, used to understand the firmware file format and partition structure. [GitHub: ramikg/airoha-firmware-parser](https://github.com/ramikg/airoha-firmware-parser).
