#!/usr/bin/env python3
"""Find unaligned little-endian 32-bit values in a binary segment."""

import argparse
import struct
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("values", nargs="+", help="integer values, normally hexadecimal")
    parser.add_argument("--load-address", type=lambda value: int(value, 0), default=0)
    args = parser.parse_args()
    data = args.binary.read_bytes()
    for text in args.values:
        value = int(text, 0)
        needle = struct.pack("<I", value)
        start = 0
        found = False
        while (offset := data.find(needle, start)) >= 0:
            found = True
            print(f"{value:#010x} file+{offset:#x} memory={args.load_address + offset:#010x}")
            start = offset + 1
        if not found:
            print(f"{value:#010x} not found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
