#!/usr/bin/env python3
"""Install a verified imDisplay firmware update over its running recovery UART."""

from __future__ import annotations

import argparse
import hashlib
import time
from pathlib import Path
from typing import Callable

import serial


MAX_FIRMWARE_BYTES = 0x1F0000
SERIAL_V1_CHUNK_BYTES = 128
SERIAL_V1_CHUNK_PAUSE_SECONDS = 0.05
SERIAL_V2_CHUNK_BYTES = 64


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def ota_header(size: int, digest: str, protocol: str, chunk_bytes: int = SERIAL_V2_CHUNK_BYTES) -> bytes:
    if size <= 0 or size > MAX_FIRMWARE_BYTES:
        raise ValueError("firmware size is outside the preserved OTA slot")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("invalid lowercase SHA-256")
    if protocol == "v1":
        return f"XSURE_OTA_V1 {size} {digest}\n".encode("ascii")
    if protocol == "v2" and 64 <= chunk_bytes <= 2048:
        return f"XSURE_OTA_V2 {size} {digest} {chunk_bytes}\n".encode("ascii")
    raise ValueError("unsupported OTA protocol or chunk size")


def read_matching_line(
    uart: serial.Serial,
    predicate: Callable[[str], bool],
    timeout_seconds: float,
) -> str | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        line = uart.readline().decode("utf-8", errors="replace").strip()
        if line.startswith("ota rejected:"):
            raise RuntimeError(line)
        if predicate(line):
            return line
    return None


def stream_firmware_v1(uart: serial.Serial, firmware: Path) -> int:
    sent = 0
    with firmware.open("rb") as handle:
        while block := handle.read(SERIAL_V1_CHUNK_BYTES):
            if uart.write(block) != len(block):
                raise RuntimeError("short UART write")
            # Keep only one small chunk in flight. The ESP32 has no wired CTS,
            # so an unpaced host stream can overflow its RX ring during flash
            # erases even though the final SHA-256 correctly rejects the image.
            uart.flush()
            time.sleep(SERIAL_V1_CHUNK_PAUSE_SECONDS)
            sent += len(block)
    return sent


def stream_firmware_v2(uart: serial.Serial, firmware: Path, chunk_bytes: int) -> int:
    sent = 0
    with firmware.open("rb") as handle:
        while block := handle.read(chunk_bytes):
            if uart.write(block) != len(block):
                raise RuntimeError("short UART write")
            uart.flush()
            sent += len(block)
            acknowledged = read_matching_line(
                uart,
                lambda line: line == f"ota chunk: {sent}",
                10.0,
            )
            if acknowledged is None:
                raise TimeoutError(f"device did not acknowledge OTA byte {sent}")
    return sent


def install(
    port: str,
    baud: int,
    firmware: Path,
    expected_banner: str,
    protocol: str,
    chunk_bytes: int,
) -> None:
    if not firmware.is_file():
        raise RuntimeError(f"missing firmware: {firmware}")
    size = firmware.stat().st_size
    digest = sha256(firmware)
    header = ota_header(size, digest, protocol, chunk_bytes)

    with serial.Serial(port, baudrate=baud, timeout=1, write_timeout=10) as uart:
        time.sleep(0.5)
        uart.write(header)
        uart.flush()
        ready_line = (
            f"ota ready: {size} bytes"
            if protocol == "v1"
            else f"ota ready: {size} bytes chunk={chunk_bytes}"
        )
        ready = read_matching_line(uart, lambda line: line == ready_line, 5.0)
        if ready is None:
            raise TimeoutError("device did not accept the OTA header")

        sent = (
            stream_firmware_v1(uart, firmware)
            if protocol == "v1"
            else stream_firmware_v2(uart, firmware, chunk_bytes)
        )
        if sent != size:
            raise RuntimeError(f"short firmware read: {sent} != {size}")

        installed = read_matching_line(
            uart,
            lambda line: line == f"ota installed: {digest}",
            45.0,
        )
        if installed is None:
            raise TimeoutError("device did not verify and install the OTA image")
        print(f"device verified SHA-256 {digest} and scheduled reboot", flush=True)

        booted = read_matching_line(uart, lambda line: line == expected_banner, 15.0)
        if booted is None:
            raise TimeoutError(f"updated firmware did not report: {expected_banner}")
        print(f"boot verified: {expected_banner}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("firmware", type=Path)
    expected = parser.add_mutually_exclusive_group(required=True)
    expected.add_argument("--expect-version")
    expected.add_argument("--expect-banner")
    parser.add_argument("--protocol", choices=("v1", "v2"), default="v2")
    parser.add_argument("--chunk-bytes", type=int, default=SERIAL_V2_CHUNK_BYTES)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baud", type=int, default=115200)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    expected_banner = args.expect_banner or f"imDisplay {args.expect_version}"
    install(args.port, args.baud, args.firmware, expected_banner, args.protocol, args.chunk_bytes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
