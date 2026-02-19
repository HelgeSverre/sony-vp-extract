#!/usr/bin/env python3
"""
Extract all Sony WH-1000XM4 voice packs into MP3 files.

Usage:
    python3 extract_all.py [input_dir] [output_dir]
    python3 extract_all.py                              # defaults: voice-packs/ → extracted/
    python3 extract_all.py voice-packs/ extracted/
"""
import struct
import lzma
import os
import sys
from Crypto.Cipher import AES

KEY = b"eibohjeCh6uegahf"
IV  = b"miefeinuShu9eilo"


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


def main():
    input_dir = sys.argv[1] if len(sys.argv) > 1 else "voice-packs"
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "extracted"

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
