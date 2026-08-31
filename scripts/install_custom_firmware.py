#!/usr/bin/env python3
"""Guarded initial X-SURE installation with exact read-back verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def require_file_hash(path: Path, expected: str) -> None:
    if not path.is_file():
        raise RuntimeError(f"missing required file: {path}")
    actual = sha256(path)
    if actual != expected:
        raise RuntimeError(f"SHA-256 mismatch for {path}: {actual} != {expected}")


def load_gate(path: Path, expected_board_mac: str) -> tuple[dict, list[tuple[int, Path]]]:
    gate = json.loads(path.read_text(encoding="utf-8"))
    if gate.get("schemaVersion") != 1:
        raise RuntimeError("unsupported write gate")
    if not re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", expected_board_mac):
        raise RuntimeError("expected board MAC must be lowercase canonical hex")
    if gate.get("boardMac") != expected_board_mac:
        raise RuntimeError("write gate is not for this ESP32")
    security = gate.get("security", {})
    if security != {
        "secureBoot": False,
        "flashEncryption": False,
        "uartDownloadEnabled": True,
    }:
        raise RuntimeError("security gate is not explicitly safe")

    backup = gate["backup"]
    first = Path(backup["first"])
    second = Path(backup["second"])
    require_file_hash(first, backup["sha256"])
    require_file_hash(second, backup["sha256"])
    if first.stat().st_size != 16 * 1024 * 1024 or second.stat().st_size != 16 * 1024 * 1024:
        raise RuntimeError("factory backups are not complete 16 MiB images")

    artifacts: list[tuple[int, Path]] = []
    for item in gate["artifacts"]:
        artifact = Path(item["path"])
        require_file_hash(artifact, item["sha256"])
        artifacts.append((int(item["address"], 0), artifact))
    if [address for address, _ in artifacts] != [0x1000, 0x8000, 0xD000, 0x10000]:
        raise RuntimeError("unexpected initial-install address map")
    return gate, artifacts


def esptool_base(esptool: Path, port: str, baud: int) -> list[str]:
    return [
        str(esptool),
        "--chip", "esp32",
        "--port", port,
        "--baud", str(baud),
        "--before", "no-reset",
        "--after", "no-reset",
    ]


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", type=Path, default=Path("xsure-backup/write-gate.json"))
    parser.add_argument("--esptool", type=Path, default=Path(".venv/bin/esptool"))
    parser.add_argument("--port", required=True)
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--expect-board-mac", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    gate, artifacts = load_gate(args.gate, args.expect_board_mac)
    pairs = [value for address, path in artifacts for value in (hex(address), str(path))]
    base = esptool_base(args.esptool, args.port, args.baud)
    print(f"gate passed for {gate['boardMac']}; writing {len(artifacts)} exact segments", flush=True)
    run(base + ["write-flash"] + pairs)
    print("write complete; verifying every installed byte", flush=True)
    run(base + ["verify-flash"] + pairs)
    print("CUSTOM FLASH VERIFIED; starting through the verified RAM bootloader", flush=True)
    # An ESP32 flasher stub cannot jump directly to flash application code. The
    # verify command's no-reset cleanup returns it to the ROM loader; loading the
    # exact installed second-stage bootloader into volatile RAM then follows the
    # normal partition-table/application path without requiring an RTS wire.
    bootloader = next(path for address, path in artifacts if address == 0x1000)
    rom_base = [
        str(args.esptool),
        "--chip", "esp32",
        "--port", args.port,
        "--baud", str(args.baud),
        "--before", "no-reset",
        "--after", "no-reset-stub",
        "--no-stub",
    ]
    run(rom_base + ["load-ram", str(bootloader)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
