#!/usr/bin/env python3
"""Capture imDisplay's owner-only boot credential into ignored local state."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

import serial


BOOT_LINE = re.compile(r"^control AP=(\S+) password=(\S+) ip=(\S+) ready=([01])$")


def parse_boot_line(line: str) -> dict[str, str | bool] | None:
    match = BOOT_LINE.fullmatch(line.strip())
    if not match:
        return None
    access_point, password, address, ready = match.groups()
    if len(password) < 12:
        return None
    return {
        "accessPoint": access_point,
        "password": password,
        "address": address,
        "ready": ready == "1",
    }


def capture(port: str, baud: int, output: Path, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    with serial.Serial(port, baudrate=baud, timeout=1) as uart:
        while time.monotonic() < deadline:
            line = uart.readline().decode("utf-8", errors="replace")
            credential = parse_boot_line(line)
            if credential is None:
                continue
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(credential, indent=2) + "\n", encoding="utf-8")
            os.chmod(output, 0o600)
            print(
                f"captured {credential['accessPoint']} at {credential['address']}; "
                f"password saved privately to {output}",
                flush=True,
            )
            return
    raise TimeoutError("device boot credential was not observed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("xsure-backup/runtime-credential.json"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    capture(args.port, args.baud, args.output, args.timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
