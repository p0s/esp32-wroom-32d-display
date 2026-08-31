#!/usr/bin/env python3
"""Provision imDisplay onto local Wi-Fi through the physically attached UART."""

from __future__ import annotations

import argparse
import getpass
import json
import re
import time
from pathlib import Path

import serial


CONNECTED = re.compile(r"^local wifi connected=1 ip=(\d{1,3}(?:\.\d{1,3}){3})$")


def load_env_file(path: Path) -> dict[str, str]:
    if path.stat().st_mode & 0o077:
        raise RuntimeError(f"credential file must be owner-only (chmod 600): {path}")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or key not in {"WIFI_NAME", "WIFI_SSID", "WIFI_PASSWORD"}:
            raise RuntimeError(f"unsupported credential entry in {path}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'\"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def make_payload(ssid: str, password: str) -> bytes:
    if not 1 <= len(ssid) <= 32:
        raise ValueError("Wi-Fi name must contain 1 to 32 characters")
    if not 8 <= len(password) <= 63:
        raise ValueError("Wi-Fi password must contain 8 to 63 characters")
    return (
        json.dumps(
            {"schema": 1, "kind": "wifi_config", "ssid": ssid, "password": password},
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("ascii")


def wait_for_line(uart: serial.Serial, expected: str, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        line = uart.readline().decode("utf-8", errors="replace").strip()
        if line == "wifi rejected":
            raise RuntimeError("device rejected local Wi-Fi settings")
        if line == expected:
            return
    raise TimeoutError(f"device did not report: {expected}")


def wait_for_address(uart: serial.Serial, timeout_seconds: int) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        line = uart.readline().decode("utf-8", errors="replace").strip()
        match = CONNECTED.fullmatch(line)
        if match:
            return match.group(1)
    raise TimeoutError("imDisplay did not connect to local Wi-Fi")


def provision(port: str, baud: int, output: Path, env_file: Path | None = None) -> None:
    credentials = load_env_file(env_file) if env_file else {}
    ssid = credentials.get("WIFI_SSID") or credentials.get("WIFI_NAME") or input(
        "Local Wi-Fi name: "
    )
    password = credentials.get("WIFI_PASSWORD") or getpass.getpass("Local Wi-Fi password: ")
    payload = make_payload(ssid, password)
    del password
    credentials.clear()
    with serial.Serial(port, baudrate=baud, timeout=1, write_timeout=5) as uart:
        time.sleep(0.5)
        uart.write(payload)
        uart.flush()
        wait_for_line(uart, "wifi saved", 5)
        print("saved privately on imDisplay; waiting for LAN connection", flush=True)
        address = wait_for_address(uart, 45)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"address": address}, indent=2) + "\n", encoding="utf-8")
    output.chmod(0o600)
    print(f"imDisplay connected at {address}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--output", type=Path, default=Path("xsure-backup/lan-state.json"))
    parser.add_argument("--env-file", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    provision(args.port, args.baud, args.output, args.env_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
