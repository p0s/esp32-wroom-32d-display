#!/usr/bin/env python3
"""Extract loadable segments from an ESP32 application image."""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path


IMAGE_MAGIC = 0xE9
IMAGE_HEADER_BYTES = 24
SEGMENT_HEADER_BYTES = 8


def classify(address: int) -> str:
    if 0x3F400000 <= address < 0x3F800000:
        return "drom"
    if 0x3FFAE000 <= address < 0x40000000:
        return "dram"
    if 0x40080000 <= address < 0x400A0000:
        return "iram"
    if 0x400D0000 <= address < 0x40400000:
        return "irom"
    if 0x50000000 <= address < 0x50002000:
        return "rtc"
    return "segment"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    image = args.image.read_bytes()
    if len(image) < IMAGE_HEADER_BYTES or image[0] != IMAGE_MAGIC:
        raise SystemExit("not an ESP32 application image")
    segment_count = image[1]
    args.output.mkdir(parents=True, exist_ok=True)
    position = IMAGE_HEADER_BYTES
    report = []
    for index in range(segment_count):
        if position + SEGMENT_HEADER_BYTES > len(image):
            raise SystemExit(f"truncated segment header {index}")
        address, length = struct.unpack_from("<II", image, position)
        position += SEGMENT_HEADER_BYTES
        end = position + length
        if end > len(image):
            raise SystemExit(f"truncated segment {index}")
        kind = classify(address)
        filename = f"segment-{index}-{kind}-{address:08x}.bin"
        (args.output / filename).write_bytes(image[position:end])
        report.append(
            {
                "index": index,
                "kind": kind,
                "loadAddress": address,
                "loadAddressHex": f"0x{address:08x}",
                "fileOffset": position,
                "fileOffsetHex": f"0x{position:x}",
                "length": length,
                "lengthHex": f"0x{length:x}",
                "file": filename,
            }
        )
        position = end
    output = json.dumps({"segmentCount": segment_count, "segments": report}, indent=2) + "\n"
    (args.output / "segments.json").write_text(output, encoding="utf-8")
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
