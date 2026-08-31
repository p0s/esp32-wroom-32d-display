#!/usr/bin/env python3
"""Download and validate imDisplay's authenticated same-renderer pixel mirror."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import struct
import urllib.request
from pathlib import Path


WIDTH = 320
HEIGHT = 240
BMP_HEADER_BYTES = 118
ROW_BYTES = WIDTH // 2
BMP_BYTES = BMP_HEADER_BYTES + ROW_BYTES * HEIGHT


def authenticated_request(url: str, password: str) -> urllib.request.Request:
    token = base64.b64encode(f"xsure:{password}".encode("utf-8")).decode("ascii")
    return urllib.request.Request(
        url,
        headers={"Authorization": f"Basic {token}", "Cache-Control": "no-store"},
    )


def read_state(address: str, password: str) -> dict[str, object]:
    request = authenticated_request(f"http://{address}/api/state", password)
    with urllib.request.urlopen(request, timeout=10) as response:
        state = json.loads(response.read())
        if response.status != 200 or not isinstance(state, dict):
            raise RuntimeError("imDisplay state readback failed")
        return state


def download_bmp(address: str, password: str) -> bytes:
    request = authenticated_request(f"http://{address}/api/screen.bmp", password)
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read()
        if response.status != 200 or response.headers.get_content_type() != "image/bmp":
            raise RuntimeError("imDisplay screen readback failed")
        return body


def decode_bmp(body: bytes) -> bytearray:
    if len(body) != BMP_BYTES or body[:2] != b"BM":
        raise RuntimeError("screen mirror is not the expected complete BMP")
    file_size = struct.unpack_from("<I", body, 2)[0]
    data_offset = struct.unpack_from("<I", body, 10)[0]
    dib_size, width, height, planes, bits, compression, image_size = struct.unpack_from(
        "<IiiHHII", body, 14
    )
    if (
        file_size != len(body)
        or data_offset != BMP_HEADER_BYTES
        or dib_size != 40
        or width != WIDTH
        or height != HEIGHT
        or planes != 1
        or bits != 4
        or compression != 0
        or image_size != ROW_BYTES * HEIGHT
    ):
        raise RuntimeError("screen mirror BMP geometry or format is invalid")

    pixels = bytearray(WIDTH * HEIGHT)
    for y in range(HEIGHT):
        source = data_offset + (HEIGHT - 1 - y) * ROW_BYTES
        target = y * WIDTH
        for packed in body[source : source + ROW_BYTES]:
            pixels[target] = packed >> 4
            pixels[target + 1] = packed & 0x0F
            target += 2
    return pixels


def presentation_key(state: dict[str, object]) -> tuple[object, ...]:
    render = state.get("render", {})
    return (
        state.get("bootId"),
        state.get("page"),
        state.get("menu"),
        state.get("valid"),
        state.get("remaining"),
        state.get("reset"),
        state.get("screen"),
        state.get("timer"),
        state.get("applets"),
        state.get("leds"),
        state.get("display"),
        state.get("sound"),
        state.get("stationConnected"),
        state.get("accessPointEnabled"),
        state.get("accessPointReady"),
        render.get("fullFrames") if isinstance(render, dict) else None,
        render.get("timerPartialUpdates") if isinstance(render, dict) else None,
        render.get("menuPartialUpdates") if isinstance(render, dict) else None,
    )


def validate_bmp(body: bytes, state: dict[str, object]) -> dict[str, object]:
    display = state.get("display", {})
    if (
        state.get("product") != "imDisplay"
        or display.get("width") != WIDTH
        or display.get("height") != HEIGHT
        or display.get("pixels") != WIDTH * HEIGHT
        or display.get("mirrorFormat") != "BMP4"
        or display.get("mirrorBytes") != ROW_BYTES * HEIGHT
        or display.get("mirrorUnknownColors") != 0
    ):
        raise RuntimeError("device did not report a complete known-color screen mirror")

    pixels = decode_bmp(body)
    counts = [0] * 16
    for pixel in pixels:
        counts[pixel] += 1
    if counts[15] or sum(counts[1:]) < 5_000:
        raise RuntimeError("screen mirror is blank or contains an unknown render color")
    if state.get("menu") is True:
        header = pixels[: 32 * WIDTH]
        body = pixels[32 * WIDTH : 216 * WIDTH]
        if sum(pixel != 0 for pixel in header) < 20 or sum(pixel == 4 for pixel in body) < 100:
            raise RuntimeError("screen mirror does not contain the launcher header and focus")
    elif pixels[0] != 4 or pixels[31 * WIDTH + WIDTH - 1] != 4:
        raise RuntimeError("screen mirror does not contain the complete blue title bar")

    if state.get("page") == "OVERVIEW" and state.get("menu") is False and state.get(
        "valid"
    ) is True:
        remaining = state.get("remaining")
        if not isinstance(remaining, (int, float)):
            raise RuntimeError("Overview state lacks a numeric quota")
        rounded = max(0, min(100, int(float(remaining) + 0.5)))
        filled = rounded * 280 // 100
        quota_index = 5 if remaining >= 40 else 6 if remaining >= 15 else 7
        bar = pixels[160 * WIDTH + 20 : 160 * WIDTH + 300]
        if any(pixel != quota_index for pixel in bar[:filled]) or any(
            pixel != 1 for pixel in bar[filled:]
        ):
            raise RuntimeError("screen mirror quota bar does not match device state")

    return {
        "width": WIDTH,
        "height": HEIGHT,
        "pixels": WIDTH * HEIGHT,
        "nonBackgroundPixels": sum(counts[1:]),
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def capture_screen(address: str, password: str, output: Path) -> dict[str, object]:
    for _ in range(2):
        before = read_state(address, password)
        body = download_bmp(address, password)
        after = read_state(address, password)
        if presentation_key(before) == presentation_key(after):
            report = validate_bmp(body, after)
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_suffix(f"{output.suffix}.tmp")
            temporary.write_bytes(body)
            temporary.chmod(0o600)
            temporary.replace(output)
            report["path"] = str(output)
            return report
    raise RuntimeError("screen presentation changed during both capture attempts")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--credential",
        type=Path,
        default=Path("xsure-backup/runtime-credential.json"),
    )
    parser.add_argument(
        "--address-file",
        type=Path,
        default=Path("xsure-backup/lan-state.json"),
    )
    parser.add_argument(
        "--output", type=Path, default=Path("xsure-backup/latest-screen.bmp")
    )
    return parser.parse_args()


def main() -> int:
    from wifi_ota import load_credential, load_private_address

    args = parse_args()
    credential = load_credential(args.credential)
    address = load_private_address(args.address_file)
    report = capture_screen(address, credential["password"], args.output)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
