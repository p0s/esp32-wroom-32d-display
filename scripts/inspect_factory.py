#!/usr/bin/env python3
"""Parse and optionally extract an ESP32 factory-flash partition table."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
from collections import Counter
from pathlib import Path
from typing import Any


PARTITION_TABLE_OFFSET = 0x8000
PARTITION_ENTRY_SIZE = 32
PARTITION_MAGIC = 0x50AA
MAX_PARTITION_ENTRIES = 96

APP_SUBTYPES = {
    0x00: "factory",
    0x10: "ota_0",
    0x11: "ota_1",
    0x12: "ota_2",
    0x13: "ota_3",
    0x20: "test",
}
DATA_SUBTYPES = {
    0x00: "ota",
    0x01: "phy",
    0x02: "nvs",
    0x03: "coredump",
    0x04: "nvs_keys",
    0x05: "efuse",
    0x06: "undefined",
    0x80: "esphttpd",
    0x81: "fat",
    0x82: "spiffs",
    0x83: "littlefs",
}


def entropy(data: bytes) -> float:
    if not data:
        return 0.0
    length = len(data)
    return -sum((count / length) * math.log2(count / length) for count in Counter(data).values())


def subtype_name(partition_type: int, subtype: int) -> str:
    if partition_type == 0:
        return APP_SUBTYPES.get(subtype, f"app_0x{subtype:02x}")
    if partition_type == 1:
        return DATA_SUBTYPES.get(subtype, f"data_0x{subtype:02x}")
    return f"0x{subtype:02x}"


def parse_partitions(flash: bytes, allow_partial: bool = False) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for index in range(MAX_PARTITION_ENTRIES):
        start = PARTITION_TABLE_OFFSET + index * PARTITION_ENTRY_SIZE
        raw = flash[start : start + PARTITION_ENTRY_SIZE]
        if len(raw) != PARTITION_ENTRY_SIZE:
            raise ValueError("truncated partition table")
        if raw == b"\xff" * PARTITION_ENTRY_SIZE:
            break
        magic = struct.unpack_from("<H", raw)[0]
        if magic == 0xEBEB:
            continue
        if magic != PARTITION_MAGIC:
            raise ValueError(f"unexpected partition magic 0x{magic:04x} at table entry {index}")
        _, partition_type, subtype, offset, size, label_raw, flags = struct.unpack("<HBBLL16sL", raw)
        label = label_raw.split(b"\0", 1)[0].decode("ascii", errors="replace")
        complete = offset + size <= len(flash)
        if not complete and not allow_partial:
            raise ValueError(f"partition {label!r} extends beyond flash image")
        contents = flash[offset : offset + size] if complete else b""
        entries.append(
            {
                "index": index,
                "label": label,
                "type": "app" if partition_type == 0 else "data" if partition_type == 1 else f"0x{partition_type:02x}",
                "subtype": subtype_name(partition_type, subtype),
                "offset": offset,
                "offsetHex": f"0x{offset:x}",
                "size": size,
                "sizeHex": f"0x{size:x}",
                "flags": flags,
                "completeInInput": complete,
                "sha256": hashlib.sha256(contents).hexdigest() if complete else None,
                "entropyBitsPerByte": round(entropy(contents), 4) if complete else None,
            }
        )
    if not entries:
        raise ValueError("no ESP32 partition entries found at 0x8000")
    return entries


def extract_partitions(flash: bytes, entries: list[dict[str, Any]], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        if not entry["completeInInput"]:
            continue
        label = entry["label"] or f"partition-{entry['index']}"
        safe_label = "".join(character if character.isalnum() or character in "-_" else "_" for character in label)
        start = entry["offset"]
        end = start + entry["size"]
        (output / f"{entry['index']:02d}-{safe_label}.bin").write_bytes(flash[start:end])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("flash", type=Path, help="complete raw ESP32 flash image")
    parser.add_argument("--extract-dir", type=Path, help="directory for exact partition binaries")
    parser.add_argument("--json-output", type=Path, help="also write the inspection report to this path")
    parser.add_argument("--allow-partial", action="store_true", help="inspect a leading partial flash image")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    flash = args.flash.read_bytes()
    if not args.allow_partial and len(flash) != 16 * 1024 * 1024:
        raise SystemExit(f"expected a 16 MiB image, got {len(flash)} bytes")
    entries = parse_partitions(flash, allow_partial=args.allow_partial)
    if args.extract_dir:
        extract_partitions(flash, entries, args.extract_dir)
    report = json.dumps({"flashBytes": len(flash), "partitionTableOffset": "0x8000", "partitions": entries}, indent=2) + "\n"
    if args.json_output:
        args.json_output.write_text(report, encoding="utf-8")
    print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
