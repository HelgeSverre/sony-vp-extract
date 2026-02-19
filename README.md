# Sony WH-1000XM4 Voice Pack Extractor

![Sony WH-1000XM4](https://img.shields.io/badge/Device-Sony%20WH--1000XM4-000000?style=flat-square&logo=sony)
![Airoha MT2811](https://img.shields.io/badge/SoC-Airoha%20MT2811-blue?style=flat-square)
![AES-128-CBC](https://img.shields.io/badge/Crypto-AES--128--CBC-red?style=flat-square)
![Languages](https://img.shields.io/badge/Languages-10-green?style=flat-square)
![Prompts](https://img.shields.io/badge/Prompts-540-orange?style=flat-square)
![License](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)
![Bun](https://img.shields.io/badge/Runtime-Bun-f472b6?style=flat-square&logo=bun)

Decrypt and extract the voice guidance MP3 prompts from Sony WH-1000XM4 encrypted firmware files.

The AES-128-CBC key was extracted by dumping the headphones' Airoha MT2811 firmware over Bluetooth Low Energy using the [RACE protocol](https://airoha.com), then disassembling the ARM Cortex-M4 FOTA decryption routine. Full technical writeup: **[docs/WRITEUP.md](docs/WRITEUP.md)**

## Quick Start

```bash
# Install Bun (if not already installed)
curl -fsSL https://bun.sh/install | bash

git clone https://github.com/helgesverre/reverse-sony-headphones.git
cd reverse-sony-headphones
bun install

# Downloads from Sony CDN automatically, then extracts
bun run cli/extract.ts --all
```

## Usage

```bash
# Extract all — auto-downloads from Sony CDN if voice-packs/ is empty
bun run cli/extract.ts --all [input-dir] [output-dir]

# Extract a single voice pack
bun run cli/extract.ts voice-packs/VP_english_UPG_03.bin extracted/

# Show voice pack info
bun run cli/extract.ts --info voice-packs/VP_english_UPG_03.bin

# Download only (no extraction)
bun run cli/extract.ts --download [output-dir]
```

### Python alternative

Using [uv](https://docs.astral.sh/uv/):

```bash
uv run --with pycryptodome extract_all.py voice-packs/ extracted/
```

Or with a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install pycryptodome
python3 extract_all.py voice-packs/ extracted/
```

## Available Languages

| Language      | File                       | Prompts | CDN                                                                                               |
| ------------- | -------------------------- | ------- | ------------------------------------------------------------------------------------------------- |
| 🇬🇧 English    | `VP_english_UPG_03.bin`    | 54      | [Download](https://info.update.sony.net/HP002/VGIDLPB0401/contents/0002/VP_english_UPG_03.bin)    |
| 🇫🇷 French     | `VP_french_UPG_03.bin`     | 54      | [Download](https://info.update.sony.net/HP002/VGIDLPB0402/contents/0002/VP_french_UPG_03.bin)     |
| 🇩🇪 German     | `VP_german_UPG_03.bin`     | 54      | [Download](https://info.update.sony.net/HP002/VGIDLPB0403/contents/0002/VP_german_UPG_03.bin)     |
| 🇪🇸 Spanish    | `VP_spanish_UPG_03.bin`    | 54      | [Download](https://info.update.sony.net/HP002/VGIDLPB0404/contents/0002/VP_spanish_UPG_03.bin)    |
| 🇮🇹 Italian    | `VP_italian_UPG_03.bin`    | 54      | [Download](https://info.update.sony.net/HP002/VGIDLPB0405/contents/0002/VP_italian_UPG_03.bin)    |
| 🇵🇹 Portuguese | `VP_portuguese_UPG_03.bin` | 54      | [Download](https://info.update.sony.net/HP002/VGIDLPB0406/contents/0002/VP_portuguese_UPG_03.bin) |
| 🇳🇱 Dutch      | `VP_dutch_UPG_03.bin`      | 54      | [Download](https://info.update.sony.net/HP002/VGIDLPB0407/contents/0002/VP_dutch_UPG_03.bin)      |
| 🇸🇪 Swedish    | `VP_swedish_UPG_03.bin`    | 54      | [Download](https://info.update.sony.net/HP002/VGIDLPB0408/contents/0002/VP_swedish_UPG_03.bin)    |
| 🇫🇮 Finnish    | `VP_finnish_UPG_03.bin`    | 54      | [Download](https://info.update.sony.net/HP002/VGIDLPB0409/contents/0002/VP_finnish_UPG_03.bin)    |
| 🇹🇷 Turkish    | `VP_turkish_UPG_03.bin`    | 54      | [Download](https://info.update.sony.net/HP002/VGIDLPB0410/contents/0002/VP_turkish_UPG_03.bin)    |

## Voice Pack Format

```
┌───────────────────────────────────────┐
│  Header (4096 bytes)                  │
│  ├── 0x000  Random nonce (32 bytes)   │
│  └── 0x100  TLV metadata             │
│       ├── compression_type = 2        │
│       ├── body_size                   │
│       ├── decompressed_size (1 MB)    │
│       └── SHA-256 hash                │
├───────────────────────────────────────┤
│  Body (offset 0x1000+)               │
│  AES-128-CBC encrypted               │
│  └── LZMA1 compressed                │
│       └── Voice guidance image (1MB)  │
│            ├── Header (version+count) │
│            ├── Entry table (54 × 8B)  │
│            │   (size, abs_offset)     │
│            └── MP3 files (48kHz mono) │
└───────────────────────────────────────┘
```

## The Key

```
AES-128-CBC Key: eibohjeCh6uegahf
AES-128-CBC IV:  miefeinuShu9eilo
```

Found hardcoded as ASCII strings in the MT2811 CM4 firmware's `.rodata` section at flash offset `0xD53A` (key) and `0xD529` (IV). Same key is used across all WH-1000XM4 units and all language packs.

## How It Was Found

1. **Connected** to the WH-1000XM4 over Bluetooth (the headphones were paired to a phone)
2. **Discovered** the RACE (Airoha Command Extensions) BLE GATT service
3. **Dumped** 59KB of CM4 firmware from flash using `RACE_STORAGE_PAGE_READ` (cmd `0x0403`)
4. **Located** the AES S-box at firmware offset `0x81F4`
5. **Determined** runtime base address `0x04200000` from ARM vector table literal pool
6. **Disassembled** the FOTA decryption function, traced key loading from literal pool

Full technical details: **[docs/WRITEUP.md](docs/WRITEUP.md)**

## Prerequisites

- [Bun](https://bun.sh) ≥ 1.0 (for the CLI tool)
- Python 3 with `lzma` module (standard library, used for LZMA decompression)

For the Python-only extractor:

- Python 3
- `pycryptodome` (`pip install pycryptodome`)

## Project Structure

```
├── cli/
│   └── extract.ts          # Bun CLI tool
├── docs/
│   └── WRITEUP.md          # Full technical writeup
├── voice-packs/            # Downloaded .bin files (gitignored)
├── extracted/              # Extracted MP3 prompts (gitignored)
├── extract_all.py          # Python alternative extractor
├── package.json
└── README.md
```

## Disclaimer

This project documents security research conducted for educational purposes on personally owned hardware. The headphones analyzed were paired and connected normally — no unauthorized access was involved.

The voice pack `.bin` files and extracted MP3 prompts are copyrighted by Sony Corporation and are **not included** in this repository. The CLI tool downloads them directly from Sony's public CDN at runtime. Do not redistribute decrypted voice packs or extracted MP3 files. This tool is provided for research and interoperability purposes only.

## License

MIT
