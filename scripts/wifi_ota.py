#!/usr/bin/env python3
"""Install imDisplay firmware through its authenticated local-Wi-Fi OTA endpoint."""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import socket
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import serial

from screen_capture import capture_screen


MAX_FIRMWARE_BYTES = 0x1F0000


def load_credential(path: Path) -> dict[str, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    required = ("accessPoint", "password", "address")
    if not all(isinstance(raw.get(key), str) and raw[key] for key in required):
        raise RuntimeError("invalid runtime credential file")
    return {key: raw[key] for key in required}


def load_private_address(path: Path) -> str:
    address = json.loads(path.read_text(encoding="utf-8")).get("address")
    if not isinstance(address, str):
        raise RuntimeError("invalid LAN state file")
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as error:
        raise RuntimeError("invalid LAN state file") from error
    if parsed.version != 4 or not parsed.is_private:
        raise RuntimeError("LAN address must be a private IPv4 address")
    return address


def multipart_firmware(firmware: Path, boundary: str) -> bytes:
    if not firmware.is_file():
        raise RuntimeError(f"missing firmware: {firmware}")
    prefix = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="firmware"; filename="firmware.bin"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("ascii")
    suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
    return prefix + firmware.read_bytes() + suffix


def firmware_metadata(firmware: Path) -> tuple[int, str]:
    if not firmware.is_file():
        raise RuntimeError(f"missing firmware: {firmware}")
    size = firmware.stat().st_size
    if not 0 < size <= MAX_FIRMWARE_BYTES:
        raise RuntimeError("firmware size is outside the preserved OTA slot")
    digest = hashlib.sha256()
    with firmware.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return size, digest.hexdigest()


def make_request(url: str, password: str, firmware: Path) -> urllib.request.Request:
    size, digest = firmware_metadata(firmware)
    boundary = f"xsure-{uuid.uuid4().hex}"
    body = multipart_firmware(firmware, boundary)
    token = base64.b64encode(f"xsure:{password}".encode("utf-8")).decode("ascii")
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query.extend((("size", str(size)), ("sha256", digest)))
    verified_url = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
    )
    return urllib.request.Request(
        verified_url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Basic {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Cache-Control": "no-store",
        },
    )


def upload_firmware(
    request: urllib.request.Request, require_digest_readback: bool = True
) -> str:
    with urllib.request.urlopen(request, timeout=120) as response:
        body = response.read().decode("utf-8", errors="replace")
        if response.status != 200 or "Update installed" not in body:
            raise RuntimeError(f"device OTA failed with HTTP {response.status}")
        if require_digest_readback and "SHA-256 verified" not in body:
            raise RuntimeError("device OTA response lacked SHA-256 verification readback")
        return body


def wait_for_banner(uart: serial.Serial, expected: str, timeout_seconds: int = 20) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        line = uart.readline().decode("utf-8", errors="replace").strip()
        if line == expected:
            print(f"boot verified: {expected}", flush=True)
            return
    raise TimeoutError(f"updated firmware did not report: {expected}")


def wait_for_device(address: str, timeout_seconds: int) -> None:
    if timeout_seconds <= 0:
        return
    print(f"waiting locally for imDisplay at {address}", flush=True)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((address, 80), timeout=2):
                print("imDisplay Wi-Fi endpoint reached", flush=True)
                return
        except OSError:
            time.sleep(1)
    raise TimeoutError("imDisplay Wi-Fi endpoint did not become reachable")


def wait_for_version(address: str, password: str, expected: str, timeout_seconds: int = 45) -> None:
    token = base64.b64encode(f"xsure:{password}".encode("utf-8")).decode("ascii")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        request = urllib.request.Request(
            f"http://{address}/api/state",
            headers={"Authorization": f"Basic {token}", "Cache-Control": "no-store"},
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                state = json.loads(response.read())
                if response.status == 200 and state.get("firmware") == expected:
                    print(f"LAN boot verified: firmware={expected}", flush=True)
                    return
        except (OSError, TimeoutError, json.JSONDecodeError):
            pass
        time.sleep(1)
    raise TimeoutError(f"updated firmware did not report over LAN: {expected}")


def run_device_self_test(address: str, password: str) -> None:
    token = base64.b64encode(f"xsure:{password}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        f"http://{address}/api/self-test?input=1",
        data=b"",
        method="POST",
        headers={"Authorization": f"Basic {token}", "Cache-Control": "no-store"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read())
        if (
            response.status != 200
            or not isinstance(result, dict)
            or result.get("pass") is not True
            or result.get("schema") != 3
            or result.get("releaseDelta") != 40
            or result.get("shortPressDelta") != 4
            or result.get("doubleClickDelta") != 18
            or result.get("longPressDelta") != 1
            or result.get("secondPressLatchDelta") != 18
            or result.get("navigationPassed") is not True
            or result.get("pageToLauncher") is not True
            or result.get("launcherToPage") is not True
            or result.get("nestedBack") is not True
            or result.get("longPressMenu") is not True
            or result.get("secondPressGrace") is not True
            or result.get("queuedPulse") is not True
            or result.get("edgeQueueHealthy") is not True
            or result.get("stateRestored") is not True
            or result.get("pageRoundTrips") != 9
            or result.get("pageRoundTripsExpected") != 9
            or result.get("backActions") != 4
            or result.get("backActionsExpected") != 4
            or result.get("failure") != "NONE"
            or result.get("failurePage") != "NONE"
        ):
            raise RuntimeError(
                "updated firmware failed its on-device input/navigation self-test"
            )
    print(
        "on-device GPIO5 gesture/navigation self-test passed: "
        "9 page/launcher round trips, 4 Back rows, queued pulse, long press, "
        "second-press grace",
        flush=True,
    )


def install(
    firmware: Path,
    credential_path: Path,
    port: str | None,
    baud: int,
    expected_banner: str | None,
    expected_version: str | None,
    wait_seconds: int,
    address_path: Path | None = None,
    self_test: bool = True,
    screen_output: Path | None = Path("xsure-backup/latest-screen.bmp"),
    require_digest_readback: bool = True,
) -> None:
    credential = load_credential(credential_path)
    if address_path:
        credential["address"] = load_private_address(address_path)
    request = make_request(f"http://{credential['address']}/update", credential["password"], firmware)
    wait_for_device(credential["address"], wait_seconds)
    if port:
        with serial.Serial(port, baudrate=baud, timeout=1) as uart:
            body = upload_firmware(request, require_digest_readback)
            print("authenticated Wi-Fi OTA accepted; waiting for reboot", flush=True)
            if expected_banner:
                wait_for_banner(uart, expected_banner)
    else:
        body = upload_firmware(request, require_digest_readback)
        print("authenticated Wi-Fi OTA accepted; waiting for reboot", flush=True)
    if "SHA-256 verified" in body:
        print("device verified exact firmware size and SHA-256", flush=True)
    else:
        print("legacy OTA receiver accepted the upload without digest readback", flush=True)
    if expected_version:
        wait_for_version(credential["address"], credential["password"], expected_version)
        if self_test:
            run_device_self_test(credential["address"], credential["password"])
        if screen_output:
            report = capture_screen(
                credential["address"], credential["password"], screen_output
            )
            print(
                f"screen readback verified: {report['width']}x{report['height']} "
                f"sha256={report['sha256']}",
                flush=True,
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("firmware", type=Path)
    parser.add_argument("--expect-banner")
    parser.add_argument("--expect-version")
    parser.add_argument(
        "--credential",
        type=Path,
        default=Path("xsure-backup/runtime-credential.json"),
    )
    parser.add_argument("--port", help="optional CP2102 port for serial boot proof")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument("--address-file", type=Path, help="private LAN-state JSON")
    parser.add_argument(
        "--skip-self-test",
        action="store_true",
        help="skip the post-boot self-test only for legacy recovery firmware",
    )
    parser.add_argument(
        "--screen-output",
        type=Path,
        default=Path("xsure-backup/latest-screen.bmp"),
        help="private authenticated pixel-mirror output",
    )
    parser.add_argument(
        "--skip-screen-readback",
        action="store_true",
        help="skip pixel readback only for legacy recovery firmware",
    )
    parser.add_argument(
        "--allow-legacy-unverified",
        action="store_true",
        help="accept missing digest readback only when restoring legacy recovery firmware",
    )
    args = parser.parse_args()
    if not args.expect_banner and not args.expect_version:
        parser.error("provide --expect-version or --expect-banner")
    if args.expect_banner and not args.port:
        parser.error("--expect-banner requires --port")
    return args


def main() -> int:
    args = parse_args()
    install(
        args.firmware,
        args.credential,
        args.port,
        args.baud,
        args.expect_banner,
        args.expect_version,
        args.wait_seconds,
        args.address_file,
        not args.skip_self_test,
        None if args.skip_screen_readback else args.screen_output,
        not args.allow_legacy_unverified,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
