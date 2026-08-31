#!/usr/bin/python3
"""Exercise the Wait=true responder with a real inherited listener."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import imdisplay_cache_responder as responder


SYSTEM_PYTHON = Path("/usr/bin/python3")
HOST_NAME = "imdisplay-mac.local"
KEY_HEX = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
KEY = bytes.fromhex(KEY_HEX)
NONCE = "0123456789abcdef0123456789abcdef"
MAX_WIRE_BYTES = 4096


def private_ipv4(value: str) -> str:
    try:
        return str(responder._private_ipv4(value, "listen address"))
    except responder.RequestError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def signed_request(port: int) -> bytes:
    authorization = hmac.new(
        KEY, responder.request_canonical(NONCE), hashlib.sha256
    ).hexdigest()
    return (
        "GET /v1/quota HTTP/1.1\r\n"
        f"Host: {HOST_NAME}:{port}\r\n"
        "Connection: close\r\n"
        "X-imDisplay-Protocol: 1\r\n"
        f"X-imDisplay-Nonce: {NONCE}\r\n"
        f"X-imDisplay-Authorization: {authorization}\r\n\r\n"
    ).encode("ascii")


def receive_response(client: socket.socket) -> bytes:
    data = bytearray()
    content_length = None
    header_end = None
    while len(data) <= MAX_WIRE_BYTES:
        chunk = client.recv(1024)
        if not chunk:
            break
        data.extend(chunk)
        if header_end is None and b"\r\n\r\n" in data:
            header_end = data.index(b"\r\n\r\n") + 4
            header_lines = bytes(data[: header_end - 4]).decode("ascii").split("\r\n")
            headers = dict(line.split(": ", 1) for line in header_lines[1:])
            content_length = int(headers["Content-Length"])
        if header_end is not None and len(data) == header_end + content_length:
            return bytes(data)
    raise RuntimeError("responder returned an incomplete or oversized response")


def verify_response(wire: bytes) -> dict[str, object]:
    headers_raw, body = wire.split(b"\r\n\r\n", 1)
    lines = headers_raw.decode("ascii").split("\r\n")
    if lines[0] != "HTTP/1.1 200 OK":
        raise RuntimeError("unexpected responder status")
    headers = dict(line.split(": ", 1) for line in lines[1:])
    body_sha256 = hashlib.sha256(body).hexdigest()
    expected = hmac.new(
        KEY, responder.response_canonical(NONCE, body_sha256), hashlib.sha256
    ).hexdigest()
    if (
        headers.get("X-imDisplay-Nonce") != NONCE
        or headers.get("X-imDisplay-Body-SHA256") != body_sha256
        or not hmac.compare_digest(headers.get("X-imDisplay-Authorization", ""), expected)
        or int(headers.get("Content-Length", "-1")) != len(body)
    ):
        raise RuntimeError("responder authentication or framing failed")
    payload = json.loads(body)
    if not payload.get("ok") or payload.get("sourceAgeSeconds", 999) > 150:
        raise RuntimeError("responder did not return fresh test data")
    return payload


def run(listen_address: str, responder_path: Path | None = None) -> dict[str, object]:
    source = (
        Path(responder.__file__).resolve()
        if responder_path is None
        else responder_path.resolve(strict=True)
    )
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="imdisplay-listener-") as temporary:
        root = Path(temporary)
        root.chmod(0o700)
        key_path = root / "read-only-key-v1"
        key_path.write_text(f"{KEY_HEX}\n", encoding="ascii")
        key_path.chmod(0o600)
        now = int(time.time())
        cache_path = root / "codex-budget.json"
        cache_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "checkedAt": now,
                    "windows": [
                        {
                            "limitId": "codex",
                            "limitName": "Codex",
                            "remainingPercent": 50,
                            "resetsAt": now + 3600,
                            "window": "primary",
                            "usedPercent": 50,
                        }
                    ],
                },
                separators=(",", ":"),
            ),
            encoding="ascii",
        )
        cache_path.chmod(0o600)

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        process = None
        try:
            listener.bind((listen_address, 0))
            listener.listen(4)
            port = listener.getsockname()[1]
            client.settimeout(5)
            client.connect((listen_address, port))
            client.sendall(signed_request(port))
            process = subprocess.Popen(
                [
                    str(SYSTEM_PYTHON),
                    "-I",
                    "-S",
                    str(source),
                    "--expected-self-sha",
                    source_sha256,
                    "--cache",
                    str(cache_path),
                    "--key-file",
                    str(key_path),
                    "--host-name",
                    HOST_NAME,
                    "--listen-address",
                    listen_address,
                    "--inherited-fd",
                    "0",
                    "--max-cache-age",
                    "150",
                ],
                stdin=listener,
                stdout=listener,
                stderr=listener,
                close_fds=True,
                env={"PATH": "/usr/bin:/bin"},
            )
            listener.close()
            wire = receive_response(client)
            payload = verify_response(wire)
            exit_code = process.wait(timeout=10)
            if exit_code != 0:
                raise RuntimeError(f"responder exited with {exit_code}")
            return {
                "status": "VERIFIED",
                "pythonFlags": ["-I", "-S"],
                "childExit": exit_code,
                "responseOk": payload["ok"],
                "wireBytes": len(wire),
                "elapsedSeconds": round(time.monotonic() - started, 3),
            }
        finally:
            client.close()
            listener.close()
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-address", type=private_ipv4, required=True)
    parser.add_argument("--responder-path", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.listen_address, args.responder_path), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
