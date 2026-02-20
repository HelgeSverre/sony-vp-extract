#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["bleak", "pycryptodome"]
# ///
"""
Extract the AES-128-CBC key from your own Sony WH-1000XM4 headphones.

This script connects to the headphones over BLE, dumps the CM4 firmware region
containing the FOTA decryption key via the RACE protocol, then searches for
the AES key and IV by finding adjacent null-terminated ASCII strings that
successfully decrypt a voice pack body into valid LZMA data.

Usage:
    # Full pipeline: dump firmware over BLE and extract key
    uv run cli/extract_key.py

    # Extract key from an existing firmware dump
    uv run cli/extract_key.py --firmware firmware.bin

    # Dump firmware only (no key search)
    uv run cli/extract_key.py --dump-only -o firmware.bin
"""
import argparse
import asyncio
import os
import struct
import sys
import time

# ── RACE protocol constants ──────────────────────────────────────────────────

SONY_SERVICE = "dc405470-a351-4a59-97d8-2e2e3b207fbb"
SONY_TX = "bfd869fa-a3f2-4c2f-bcff-3eb1ec80cead"
SONY_RX = "2a6b6575-faf6-418c-923f-ccd63a56d955"

RACE_HEAD = 0x05
RACE_CMD_EXPECTS_RESPONSE = 0x5A
RACE_HEADER_SIZE = 6

CMD_STORAGE_PAGE_READ = 0x0403

# ── AES key search constants ────────────────────────────────────────────────

AES_SBOX = bytes([
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5,
    0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
])

VOICE_PACK_URL = "https://info.update.sony.net/HP002/VGIDLPB0401/contents/0002/VP_english_UPG_03.bin"

# ── BLE flash dumper ─────────────────────────────────────────────────────────


class RaceClient:
    def __init__(self, client, tx_char, rx_char):
        self.client = client
        self.tx_char = tx_char
        self.rx_char = rx_char
        self.recv_buf = bytearray()
        self.expected_length = None
        self.response_ready = asyncio.Event()
        self.last_response = None
        self._partial_timer = None

    async def start(self):
        await self.client.start_notify(self.rx_char, self._on_notification)

    async def stop(self):
        await self.client.stop_notify(self.rx_char)

    def _on_notification(self, _sender, data):
        self.recv_buf.extend(data)

        if self.expected_length is None:
            if len(self.recv_buf) >= RACE_HEADER_SIZE:
                self.expected_length = struct.unpack_from("<H", self.recv_buf, 2)[0]
            else:
                return

        if len(self.recv_buf) >= 4 + self.expected_length:
            self._accept()
        else:
            if self._partial_timer:
                self._partial_timer.cancel()
            loop = asyncio.get_event_loop()
            self._partial_timer = loop.call_later(0.3, self._accept)

    def _accept(self):
        if not self.response_ready.is_set():
            self.last_response = bytes(self.recv_buf)
            self.response_ready.set()
        self.recv_buf = bytearray()
        self.expected_length = None

    async def read_flash_page(self, address: int, storage_type: int = 0x00) -> bytes | None:
        self.response_ready.clear()
        self.last_response = None
        self.recv_buf = bytearray()
        self.expected_length = None

        payload = struct.pack("<BBI", storage_type, 0x01, address)
        length = len(payload) + 2
        packet = struct.pack("<BBHH", RACE_HEAD, RACE_CMD_EXPECTS_RESPONSE, length, CMD_STORAGE_PAGE_READ) + payload

        await self.client.write_gatt_char(self.tx_char, packet, response=False)

        try:
            await asyncio.wait_for(self.response_ready.wait(), 15.0)
        except asyncio.TimeoutError:
            return None

        resp = self.last_response
        if resp is None or len(resp) < RACE_HEADER_SIZE + 2:
            return None
        if resp[RACE_HEADER_SIZE] != 0:
            return None

        return resp[RACE_HEADER_SIZE + 8:]


async def scan_for_headphones() -> str | None:
    from bleak import BleakScanner

    print("scanning for WH-1000XM4...")
    devices = await BleakScanner.discover(timeout=10, return_adv=True)

    candidates = []
    for addr, (device, adv) in devices.items():
        name = adv.local_name or device.name or ""
        if "wh-1000xm4" in name.lower():
            candidates.append((addr, name, adv.rssi))
            print(f"  found: {name} ({addr}) RSSI={adv.rssi}")

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[2], reverse=True)
    return candidates[0][0]


async def dump_firmware(output_path: str | None = None) -> bytes:
    from bleak import BleakClient as BClient, BleakScanner

    target = await scan_for_headphones()
    if not target:
        print("error: no WH-1000XM4 found. make sure headphones are powered on.")
        sys.exit(1)

    print(f"connecting to {target}...")
    async with BClient(target, timeout=20) as client:
        print(f"connected (MTU: {client.mtu_size})")

        tx_char = rx_char = None
        for service in client.services:
            if str(service.uuid).lower() == SONY_SERVICE:
                for char in service.characteristics:
                    cuuid = str(char.uuid).lower()
                    if cuuid == SONY_TX:
                        tx_char = char
                    elif cuuid == SONY_RX:
                        rx_char = char

        if not tx_char or not rx_char:
            print("error: RACE service not found on this device")
            sys.exit(1)

        race = RaceClient(client, tx_char, rx_char)
        await race.start()

        # CM4 firmware lives at physical flash address 0x08002000.
        # With storage_type=0x00 (physical addressing), we read from 0x2000.
        flash_base = 0x2000
        total_size = 60 * 1024
        chunk_size = 225  # max payload per BLE response
        num_reads = (total_size + chunk_size - 1) // chunk_size

        print(f"dumping {total_size // 1024}KB of CM4 firmware from 0x{flash_base:04X} ({num_reads} reads)...")
        t_start = time.time()
        fails = 0
        bytes_written = 0

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "wb") as f:
            for i in range(num_reads):
                addr = flash_base + i * chunk_size
                page = await race.read_flash_page(addr, storage_type=0x00)

                if page is None:
                    chunk = b"\xff" * chunk_size
                    fails += 1
                    if fails >= 10:
                        print("\nerror: too many consecutive failures, aborting")
                        break
                else:
                    chunk = page[:chunk_size]
                    fails = 0

                remaining = total_size - bytes_written
                write_len = min(len(chunk), remaining)
                f.write(chunk[:write_len])
                f.flush()
                bytes_written += write_len

                if bytes_written >= total_size:
                    break

                if i % 16 == 0 or i == num_reads - 1:
                    pct = bytes_written / total_size * 100
                    elapsed = time.time() - t_start
                    rate = (i + 1) / elapsed if elapsed > 0 else 0
                    eta = (num_reads - i - 1) / rate if rate > 0 else 0
                    print(f"\r  [{pct:5.1f}%] {bytes_written // 1024}KB | {rate:.1f} reads/s | ETA {eta:.0f}s", end="", flush=True)

        await race.stop()

    elapsed = time.time() - t_start
    print(f"\n  done in {elapsed:.0f}s ({bytes_written} bytes)")
    print(f"  saved to {output_path}")

    return open(output_path, "rb").read()


# ── Key search ───────────────────────────────────────────────────────────────


def find_aes_sbox(firmware: bytes) -> int | None:
    idx = firmware.find(AES_SBOX)
    return idx if idx >= 0 else None


def find_ascii16_strings(firmware: bytes) -> list[tuple[int, bytes]]:
    """Find all 16-byte null-terminated printable ASCII strings."""
    results = []
    for i in range(len(firmware) - 16):
        chunk = firmware[i : i + 16]
        if all(32 < b < 127 for b in chunk):
            if i + 16 < len(firmware) and firmware[i + 16] == 0x00:
                results.append((i, chunk))
    return results


def try_key_iv_pair(key: bytes, iv: bytes, ciphertext: bytes) -> bool:
    """Test if key+IV decrypt ciphertext into valid LZMA1 data."""
    from Crypto.Cipher import AES as AESCipher

    cipher = AESCipher.new(key, AESCipher.MODE_CBC, iv)
    dec = cipher.decrypt(ciphertext[:16])
    if dec[0] != 0x5D:  # LZMA props: lc=3, lp=0, pb=2
        return False
    dict_size = struct.unpack_from("<I", dec, 1)[0]
    return dict_size == 16384


def get_voice_pack_body() -> bytes:
    """Get the first 4KB of encrypted voice pack body for validation."""
    local_path = os.path.join("voice-packs", "VP_english_UPG_03.bin")
    if os.path.exists(local_path) and os.path.getsize(local_path) > 0x1100:
        data = open(local_path, "rb").read()
        return data[0x1000 : 0x1000 + 4096]

    print("downloading voice pack sample for key validation...")
    import urllib.request

    req = urllib.request.Request(VOICE_PACK_URL)
    req.add_header("Range", "bytes=4096-8191")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.read()
    except Exception:
        # Range not supported, download full file
        with urllib.request.urlopen(VOICE_PACK_URL) as resp:
            data = resp.read()
        os.makedirs("voice-packs", exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(data)
        return data[0x1000 : 0x1000 + 4096]


def search_for_key(firmware: bytes) -> tuple[str, str, int, int] | None:
    """Search firmware for AES key and IV. Returns (key, iv, key_offset, iv_offset) or None."""
    sbox_offset = find_aes_sbox(firmware)
    if sbox_offset is not None:
        print(f"  AES S-box found at offset 0x{sbox_offset:04X}")
    else:
        print("  warning: AES S-box not found (firmware may be incomplete)")

    candidates = find_ascii16_strings(firmware)
    print(f"  found {len(candidates)} candidate 16-byte ASCII strings")

    if len(candidates) < 2:
        print("  error: not enough candidates to test")
        return None

    body = get_voice_pack_body()
    print(f"  testing key/IV pairs against voice pack ciphertext...")

    for ci, (off_a, str_a) in enumerate(candidates):
        for off_b, str_b in candidates[ci + 1 : ci + 6]:
            # Try a=IV, b=key
            if try_key_iv_pair(str_b, str_a, body):
                return (str_b.decode("ascii"), str_a.decode("ascii"), off_b, off_a)
            # Try b=IV, a=key
            if try_key_iv_pair(str_a, str_b, body):
                return (str_a.decode("ascii"), str_b.decode("ascii"), off_a, off_b)

    return None


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Extract AES key from Sony WH-1000XM4 firmware",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  uv run cli/extract_key.py                          # BLE dump + extract key
  uv run cli/extract_key.py --firmware firmware.bin   # find key in existing dump
  uv run cli/extract_key.py --dump-only -o fw.bin     # dump firmware only
""",
    )
    parser.add_argument("--firmware", "-f", help="path to existing firmware dump (skip BLE)")
    parser.add_argument("--dump-only", action="store_true", help="dump firmware and exit")
    parser.add_argument("-o", "--output", default="firmware_dump.bin", help="output path for firmware dump")
    parser.add_argument("--json", action="store_true", help="output key as JSON")
    args = parser.parse_args()

    if args.firmware:
        print(f"loading firmware from {args.firmware}")
        firmware = open(args.firmware, "rb").read()
        print(f"  {len(firmware)} bytes")
    else:
        firmware = asyncio.run(dump_firmware(args.output))

    if args.dump_only:
        return

    print("\nsearching for AES key...")
    result = search_for_key(firmware)

    if result is None:
        print("\nno valid AES key found in firmware.")
        sys.exit(1)

    key, iv, key_off, iv_off = result

    if args.json:
        import json

        print(json.dumps({"key": key, "iv": iv, "key_offset": f"0x{key_off:04X}", "iv_offset": f"0x{iv_off:04X}"}))
    else:
        print(f"\n{'=' * 44}")
        print(f"  AES-128-CBC Key:  {key}")
        print(f"  AES-128-CBC IV:   {iv}")
        print(f"{'=' * 44}")
        print(f"  key offset: 0x{key_off:04X}  (runtime: 0x{0x04200000 + key_off:08X})")
        print(f"  IV offset:  0x{iv_off:04X}  (runtime: 0x{0x04200000 + iv_off:08X})")


if __name__ == "__main__":
    main()
