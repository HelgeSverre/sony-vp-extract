#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pycryptodome"]
# ///
"""
Download and extract all Sony WH-1000XM4 voice packs into MP3 files.

Usage:
    uv run extract_all.py                              # downloads + extracts everything
    uv run extract_all.py [input_dir] [output_dir]
"""
import struct
import lzma
import os
import sys
import urllib.request
from pathlib import Path
from Crypto.Cipher import AES

KEY = b"eibohjeCh6uegahf"
IV  = b"miefeinuShu9eilo"

VOICE_PACK_URLS = {
    "english":    "https://info.update.sony.net/HP002/VGIDLPB0401/contents/0002/VP_english_UPG_03.bin",
    "french":     "https://info.update.sony.net/HP002/VGIDLPB0402/contents/0002/VP_french_UPG_03.bin",
    "german":     "https://info.update.sony.net/HP002/VGIDLPB0403/contents/0002/VP_german_UPG_03.bin",
    "spanish":    "https://info.update.sony.net/HP002/VGIDLPB0404/contents/0002/VP_spanish_UPG_03.bin",
    "italian":    "https://info.update.sony.net/HP002/VGIDLPB0405/contents/0002/VP_italian_UPG_03.bin",
    "portuguese": "https://info.update.sony.net/HP002/VGIDLPB0406/contents/0002/VP_portuguese_UPG_03.bin",
    "dutch":      "https://info.update.sony.net/HP002/VGIDLPB0407/contents/0002/VP_dutch_UPG_03.bin",
    "swedish":    "https://info.update.sony.net/HP002/VGIDLPB0408/contents/0002/VP_swedish_UPG_03.bin",
    "finnish":    "https://info.update.sony.net/HP002/VGIDLPB0409/contents/0002/VP_finnish_UPG_03.bin",
    "turkish":    "https://info.update.sony.net/HP002/VGIDLPB0410/contents/0002/VP_turkish_UPG_03.bin",
}


def extract_voice_pack(input_path: str, output_dir: str) -> int:
    data = open(input_path, "rb").read()
    body = data[0x1000:]

    # AES-128-CBC decrypt
    cipher = AES.new(KEY, AES.MODE_CBC, IV)
    dec = cipher.decrypt(body)

    # LZMA decompress (skip 13-byte LZMA header, use raw LZMA1)
    props = dec[0]
    lc = props % 9
    remainder = props // 9
    lp = remainder % 5
    pb = remainder // 5
    dict_size = struct.unpack_from("<I", dec, 1)[0]

    filters = [{"id": lzma.FILTER_LZMA1, "lc": lc, "lp": lp, "pb": pb, "dict_size": dict_size}]
    decompressor = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filters)
    decompressed = decompressor.decompress(dec[13:], max_length=0x100000)

    # Parse entry table
    num_entries = struct.unpack_from("<I", decompressed, 4)[0]

    # Derive base offset from first entry
    first_abs_offset = struct.unpack_from("<I", decompressed, 8 + 4)[0]
    table_end = 8 + num_entries * 8
    base_offset = first_abs_offset - table_end

    os.makedirs(output_dir, exist_ok=True)
    extracted = 0

    for i in range(num_entries):
        off = 8 + i * 8
        size = struct.unpack_from("<I", decompressed, off)[0]
        offset_abs = struct.unpack_from("<I", decompressed, off + 4)[0]
        file_offset = offset_abs - base_offset

        if file_offset < 0 or file_offset + size > len(decompressed):
            continue

        prompt_data = decompressed[file_offset : file_offset + size]
        out_path = os.path.join(output_dir, f"prompt_{i:02d}.mp3")
        with open(out_path, "wb") as f:
            f.write(prompt_data)
        extracted += 1

    return extracted


def download_voice_packs(output_dir: str):
    os.makedirs(output_dir, exist_ok=True)
    print("Downloading voice packs from Sony CDN...\n")
    for lang, url in VOICE_PACK_URLS.items():
        filename = url.split("/")[-1]
        out_path = os.path.join(output_dir, filename)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            print(f"  --  {filename} (cached)")
            continue
        print(f"  ⬇   {filename}...", end="", flush=True)
        urllib.request.urlretrieve(url, out_path)
        size_kb = os.path.getsize(out_path) / 1024
        print(f" {size_kb:.0f} KB")
    print()


def main():
    input_dir = sys.argv[1] if len(sys.argv) > 1 else "voice-packs"
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "extracted"

    os.makedirs(input_dir, exist_ok=True)
    bin_files = sorted(f for f in os.listdir(input_dir) if f.endswith(".bin"))

    if not bin_files:
        download_voice_packs(input_dir)
        bin_files = sorted(f for f in os.listdir(input_dir) if f.endswith(".bin"))

    if not bin_files:
        print(f"No .bin files found in {input_dir}/")
        sys.exit(1)

    print(f"Extracting {len(bin_files)} voice packs...\n")

    total = 0
    for fname in bin_files:
        lang = fname.replace("VP_", "").split("_UPG_")[0]
        lang_dir = os.path.join(output_dir, lang)
        count = extract_voice_pack(os.path.join(input_dir, fname), lang_dir)
        total += count
        print(f"  ✅ {lang:12s} → {count} prompts")

    print(f"\nDone! {total} total prompts extracted to {output_dir}/")


if __name__ == "__main__":
    main()
