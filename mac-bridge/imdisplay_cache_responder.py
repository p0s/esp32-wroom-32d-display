#!/usr/bin/python3
"""Serve a bounded burst of authenticated imDisplay quota-cache requests."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import socket
import stat
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


PROTOCOL_VERSION = 1
CACHE_SCHEMA_VERSION = 1
DEFAULT_MAX_CACHE_AGE_SECONDS = 150
MAX_FUTURE_SKEW_SECONDS = 30
MAX_CACHE_BYTES = 16 * 1024
MAX_KEY_BYTES = 65
MAX_REQUEST_BYTES = 2048
MAX_RESPONSE_BODY_BYTES = 2047
MAX_SELF_BYTES = 128 * 1024
MAX_WINDOWS = 6
SOCKET_TIMEOUT_SECONDS = 2.0
LISTENER_IDLE_TIMEOUT_SECONDS = 3.0
MAX_LISTENER_LIFETIME_SECONDS = 20.0
MAX_CONNECTIONS = 8
REQUEST_PATH = "/v1/quota"
REQUEST_CANONICAL_PREFIX = "imdisplay-cache-v1\nrequest\nGET\n/v1/quota\n"
RESPONSE_CANONICAL_PREFIX = "imdisplay-cache-v1\nresponse\n200\n"
LOWER_NONCE = re.compile(r"[0-9a-f]{32}\Z")
LOWER_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
LOCAL_HOSTNAME = re.compile(r"(?=.{7,63}\Z)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.local\Z")
PRIVATE_DEVICE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


class ResponderError(Exception):
    """An expected fail-closed responder error."""


class CacheError(ResponderError):
    """The atomic quota cache is absent or invalid."""


class StaleCacheError(CacheError):
    def __init__(self, checked_at: int, age_seconds: int) -> None:
        super().__init__("quota cache is stale")
        self.checked_at = checked_at
        self.age_seconds = age_seconds


class RequestError(ResponderError):
    """The inherited client or request failed authentication or validation."""


def _private_ipv4(value: str, label: str) -> ipaddress.IPv4Address:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise RequestError(f"invalid {label}") from error
    if not isinstance(address, ipaddress.IPv4Address) or not any(
        address in network for network in PRIVATE_DEVICE_NETWORKS
    ):
        raise RequestError(f"{label} must be a private IPv4 address")
    return address


def _pull_host(value: str) -> str:
    if LOCAL_HOSTNAME.fullmatch(value):
        return value
    try:
        return str(_private_ipv4(value, "pull host"))
    except RequestError as error:
        raise RequestError("pull host must be a strict lowercase .local name or private IPv4") from error


def _check_owner(metadata: os.stat_result, expected_type: int, label: str) -> None:
    if stat.S_IFMT(metadata.st_mode) != expected_type:
        raise CacheError(f"{label} has the wrong file type")
    if metadata.st_uid != os.geteuid():
        raise CacheError(f"{label} has the wrong owner")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise CacheError(f"{label} is accessible outside its owner")


def _check_private_parent(path: Path, label: str) -> None:
    try:
        metadata = os.lstat(path.parent)
    except OSError as error:
        raise CacheError(f"{label} directory is unavailable") from error
    _check_owner(metadata, stat.S_IFDIR, f"{label} directory")


def read_owner_only_file(path: Path, maximum_bytes: int, label: str) -> tuple[bytes, os.stat_result]:
    _check_private_parent(path, label)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError as error:
        raise CacheError(f"{label} is unavailable") from error
    try:
        before = os.fstat(descriptor)
        _check_owner(before, stat.S_IFREG, label)
        if before.st_nlink != 1 or before.st_size <= 0 or before.st_size > maximum_bytes:
            raise CacheError(f"{label} violates size or link bounds")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(4096, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_identity != after_identity or len(data) != before.st_size:
        raise CacheError(f"{label} changed during read")
    if len(data) > maximum_bytes:
        raise CacheError(f"{label} is too large")
    return data, before


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_strict_json(data: bytes) -> Any:
    try:
        return json.loads(
            data.decode("utf-8"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise CacheError("invalid JSON") from error


def _finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _bounded_ascii(value: object, label: str, maximum: int, nullable: bool = False) -> Optional[str]:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise CacheError(f"invalid {label}")
    if not value.isascii() or any(ord(character) < 0x20 for character in value):
        raise CacheError(f"invalid {label}")
    return value


def validate_cache_schema(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"schemaVersion", "checkedAt", "windows"}:
        raise CacheError("unsupported quota cache schema")
    if raw["schemaVersion"] != CACHE_SCHEMA_VERSION:
        raise CacheError("unsupported quota cache version")
    checked_at = raw["checkedAt"]
    if isinstance(checked_at, bool) or not isinstance(checked_at, int) or checked_at <= 0:
        raise CacheError("invalid cache timestamp")
    windows = raw["windows"]
    if not isinstance(windows, list) or not 1 <= len(windows) <= MAX_WINDOWS:
        raise CacheError("invalid quota window count")
    required = {"limitId", "limitName", "remainingPercent", "resetsAt", "window"}
    allowed = required | {"usedPercent"}
    seen: set[tuple[str, str]] = set()
    for item in windows:
        if not isinstance(item, dict) or not required.issubset(item) or not set(item).issubset(allowed):
            raise CacheError("invalid quota window schema")
        limit_id = _bounded_ascii(item["limitId"], "limit id", 32)
        _bounded_ascii(item["limitName"], "limit name", 31, nullable=True)
        window = _bounded_ascii(item["window"], "window name", 19)
        identity = (limit_id or "", window or "")
        if identity in seen:
            raise CacheError("duplicate quota window")
        seen.add(identity)
        remaining = item["remainingPercent"]
        if not _finite_number(remaining) or not 0 <= float(remaining) <= 100:
            raise CacheError("invalid remaining percentage")
        if "usedPercent" in item:
            used = item["usedPercent"]
            if not _finite_number(used) or not 0 <= float(used) <= 100:
                raise CacheError("invalid used percentage")
        resets_at = item["resetsAt"]
        if resets_at is not None and (
            isinstance(resets_at, bool)
            or not isinstance(resets_at, int)
            or not 0 < resets_at <= 4_102_444_800
        ):
            raise CacheError("invalid reset timestamp")
    return raw


def load_quota_cache(path: Path, now: int, max_age_seconds: int) -> tuple[dict[str, Any], int]:
    data, metadata = read_owner_only_file(path, MAX_CACHE_BYTES, "quota cache")
    raw = validate_cache_schema(parse_strict_json(data))
    checked_at = raw["checkedAt"]
    modified_at = int(metadata.st_mtime)
    if checked_at > now + MAX_FUTURE_SKEW_SECONDS or modified_at > now + MAX_FUTURE_SKEW_SECONDS:
        raise CacheError("quota cache is from the future")
    age_seconds = max(0, now - checked_at, now - modified_at)
    if age_seconds > max_age_seconds:
        raise StaleCacheError(checked_at, age_seconds)
    return raw, age_seconds


def load_key(path: Path) -> bytes:
    data, _ = read_owner_only_file(path, MAX_KEY_BYTES, "read-only key")
    encoded = data[:-1] if data.endswith(b"\n") else data
    try:
        text = encoded.decode("ascii")
    except UnicodeDecodeError as error:
        raise CacheError("invalid read-only key") from error
    if not LOWER_SHA256.fullmatch(text):
        raise CacheError("invalid read-only key")
    return bytes.fromhex(text)


def _local_time_text(epoch: Optional[int]) -> str:
    if epoch is None or epoch <= 0:
        return "Unknown"
    return datetime.fromtimestamp(epoch).astimezone().strftime("%d %b %H:%M")


def make_payload(raw: dict[str, Any], age_seconds: int) -> dict[str, Any]:
    raw = validate_cache_schema(raw)
    windows: list[dict[str, Any]] = []
    for index, item in enumerate(raw["windows"]):
        resets_at = item["resetsAt"]
        is_codex = item["limitId"] == "codex"
        display_window = (
            item["window"]
            if item["window"] in {"primary", "secondary"}
            else f"window {index + 1}"
        )
        windows.append(
            {
                "id": f"codex:{display_window}" if is_codex else f"quota:{index + 1}",
                "label": "Codex" if is_codex else "Quota",
                "window": display_window,
                "remaining": round(float(item["remainingPercent"]), 1),
                "resetsAt": resets_at,
                "resetText": _local_time_text(resets_at),
            }
        )
    checked_at = raw["checkedAt"]
    return {
        "schema": 1,
        "kind": "codex_budget",
        "ok": True,
        "stale": False,
        "checkedAt": checked_at,
        "checkedText": _local_time_text(checked_at),
        "sourceAgeSeconds": age_seconds,
        "windows": windows,
    }


def make_unavailable_payload(
    stale: bool, checked_at: Optional[int] = None, age_seconds: Optional[int] = None
) -> dict[str, Any]:
    return {
        "schema": 1,
        "kind": "codex_budget",
        "ok": False,
        "stale": stale,
        "checkedAt": checked_at,
        "checkedText": _local_time_text(checked_at),
        "sourceAgeSeconds": age_seconds,
        "windows": [],
        "error": "Stale data" if stale else "No data",
    }


def cache_payload(path: Path, now: int, max_age_seconds: int) -> dict[str, Any]:
    try:
        raw, age_seconds = load_quota_cache(path, now, max_age_seconds)
        return make_payload(raw, age_seconds)
    except StaleCacheError as error:
        return make_unavailable_payload(True, error.checked_at, error.age_seconds)
    except CacheError:
        return make_unavailable_payload(False)


def encode_payload(payload: dict[str, Any]) -> bytes:
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(encoded) > MAX_RESPONSE_BODY_BYTES:
        raise CacheError("device payload exceeds its size bound")
    return encoded


def request_canonical(nonce: str) -> bytes:
    if not LOWER_NONCE.fullmatch(nonce):
        raise RequestError("invalid nonce")
    return f"{REQUEST_CANONICAL_PREFIX}{nonce}\n".encode("ascii")


def response_canonical(nonce: str, body_sha256: str) -> bytes:
    if not LOWER_NONCE.fullmatch(nonce) or not LOWER_SHA256.fullmatch(body_sha256):
        raise RequestError("invalid response identity")
    return f"{RESPONSE_CANONICAL_PREFIX}{nonce}\n{body_sha256}\n".encode("ascii")


def parse_request(data: bytes, key: bytes, expected_host: str) -> str:
    if len(data) > MAX_REQUEST_BYTES or not data.endswith(b"\r\n\r\n"):
        raise RequestError("invalid request framing")
    try:
        lines = data[:-4].decode("ascii").split("\r\n")
    except UnicodeDecodeError as error:
        raise RequestError("request is not ASCII") from error
    if not lines or lines[0] != f"GET {REQUEST_PATH} HTTP/1.1":
        raise RequestError("invalid request line")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            raise RequestError("malformed request header")
        name, value = line.split(":", 1)
        name = name.lower()
        value = value.strip()
        if name in headers:
            raise RequestError("duplicate request header")
        headers[name] = value
    required = {
        "host",
        "connection",
        "x-imdisplay-protocol",
        "x-imdisplay-nonce",
        "x-imdisplay-authorization",
    }
    if set(headers) != required:
        raise RequestError("unexpected request headers")
    host = headers["host"]
    if (
        not host
        or len(host) > 64
        or any(ord(character) < 0x21 for character in host)
        or host != expected_host
    ):
        raise RequestError("invalid host header")
    if headers["connection"].lower() != "close" or headers["x-imdisplay-protocol"] != "1":
        raise RequestError("unsupported request protocol")
    nonce = headers["x-imdisplay-nonce"]
    authorization = headers["x-imdisplay-authorization"]
    if not LOWER_NONCE.fullmatch(nonce) or not LOWER_SHA256.fullmatch(authorization):
        raise RequestError("invalid authentication header")
    expected = hmac.new(key, request_canonical(nonce), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(authorization, expected):
        raise RequestError("request authentication failed")
    return nonce


def receive_request(connection: socket.socket) -> bytes:
    buffer = bytearray()
    while b"\r\n\r\n" not in buffer:
        if len(buffer) >= MAX_REQUEST_BYTES:
            raise RequestError("request headers are too large")
        chunk = connection.recv(min(512, MAX_REQUEST_BYTES + 1 - len(buffer)))
        if not chunk:
            raise RequestError("request closed before its headers")
        buffer.extend(chunk)
    marker = buffer.find(b"\r\n\r\n") + 4
    if marker != len(buffer):
        raise RequestError("request body or pipelining is forbidden")
    return bytes(buffer)


def validate_connection(connection: socket.socket) -> None:
    if connection.family != socket.AF_INET:
        raise RequestError("inherited descriptor is not an IPv4 stream socket")
    try:
        socket_type = connection.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
        peer = connection.getpeername()
        local = connection.getsockname()
    except OSError as error:
        raise RequestError("inherited descriptor is not a connected socket") from error
    if socket_type != socket.SOCK_STREAM:
        raise RequestError("inherited descriptor is not an IPv4 stream socket")
    if not isinstance(peer, tuple) or not isinstance(local, tuple):
        raise RequestError("invalid inherited socket addresses")
    _private_ipv4(peer[0], "client address")
    _private_ipv4(local[0], "local address")


def validate_listener(listener: socket.socket, listen_address: str) -> None:
    if listener.family != socket.AF_INET:
        raise RequestError("inherited descriptor is not an IPv4 stream socket")
    try:
        socket_type = listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
        local = listener.getsockname()
    except OSError as error:
        raise RequestError("inherited descriptor is not a listening socket") from error
    if socket_type != socket.SOCK_STREAM or not isinstance(local, tuple):
        raise RequestError("inherited descriptor is not an IPv4 stream socket")
    expected = _private_ipv4(listen_address, "listen address")
    actual = _private_ipv4(local[0], "listener address")
    if actual != expected:
        raise RequestError("listener address does not match configuration")
    try:
        listener.getpeername()
    except OSError:
        pass
    else:
        raise RequestError("inherited descriptor is a connected socket")


def build_response(nonce: str, body: bytes, key: bytes) -> bytes:
    if len(body) > MAX_RESPONSE_BODY_BYTES:
        raise CacheError("device payload exceeds its size bound")
    body_sha256 = hashlib.sha256(body).hexdigest()
    authorization = hmac.new(
        key, response_canonical(nonce, body_sha256), hashlib.sha256
    ).hexdigest()
    headers = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "X-imDisplay-Protocol: 1\r\n"
        f"X-imDisplay-Nonce: {nonce}\r\n"
        f"X-imDisplay-Body-SHA256: {body_sha256}\r\n"
        f"X-imDisplay-Authorization: {authorization}\r\n"
        "\r\n"
    ).encode("ascii")
    return headers + body


def _send_empty_error(connection: socket.socket) -> None:
    try:
        connection.sendall(
            b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        )
    except OSError:
        pass


def serve_once(
    connection: socket.socket,
    cache_path: Path,
    key_path: Path,
    host_name: str,
    max_cache_age_seconds: int,
    now: Optional[int] = None,
    timeout_seconds: float = SOCKET_TIMEOUT_SECONDS,
) -> bool:
    if not 0 < timeout_seconds <= SOCKET_TIMEOUT_SECONDS:
        raise RequestError("invalid request timeout")
    connection.settimeout(timeout_seconds)
    try:
        validate_connection(connection)
        key = load_key(key_path)
        local = connection.getsockname()
        nonce = parse_request(
            receive_request(connection), key, f"{_pull_host(host_name)}:{local[1]}"
        )
        payload = cache_payload(cache_path, int(time.time()) if now is None else now, max_cache_age_seconds)
        connection.sendall(build_response(nonce, encode_payload(payload), key))
        return True
    except (OSError, ResponderError):
        _send_empty_error(connection)
        return False


def serve_listener(
    listener: socket.socket,
    cache_path: Path,
    key_path: Path,
    host_name: str,
    listen_address: str,
    max_cache_age_seconds: int,
    now: Optional[int] = None,
    *,
    max_connections: int = MAX_CONNECTIONS,
    idle_timeout_seconds: float = LISTENER_IDLE_TIMEOUT_SECONDS,
    max_lifetime_seconds: float = MAX_LISTENER_LIFETIME_SECONDS,
) -> tuple[int, int]:
    if (
        not 1 <= max_connections <= MAX_CONNECTIONS
        or not 0 < idle_timeout_seconds <= LISTENER_IDLE_TIMEOUT_SECONDS
        or not 0 < max_lifetime_seconds <= MAX_LISTENER_LIFETIME_SECONDS
    ):
        raise RequestError("invalid listener bounds")
    validate_listener(listener, listen_address)
    started = time.monotonic()
    accepted = 0
    succeeded = 0
    while accepted < max_connections:
        remaining = max_lifetime_seconds - (time.monotonic() - started)
        if remaining <= 0:
            break
        listener.settimeout(min(idle_timeout_seconds, remaining))
        connection: Optional[socket.socket] = None
        try:
            connection, _ = listener.accept()
        except socket.timeout:
            break
        except OSError as error:
            raise RequestError("unable to accept inherited connection") from error
        accepted += 1
        try:
            request_budget = min(
                SOCKET_TIMEOUT_SECONDS,
                max_lifetime_seconds - (time.monotonic() - started),
            )
            if request_budget <= 0:
                break
            if serve_once(
                connection,
                cache_path,
                key_path,
                host_name,
                max_cache_age_seconds,
                now,
                request_budget,
            ):
                succeeded += 1
        finally:
            connection.close()
    return accepted, succeeded


def verify_self(path: Path, expected_sha256: str) -> None:
    if not LOWER_SHA256.fullmatch(expected_sha256):
        raise ResponderError("invalid expected self hash")
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise ResponderError("responder source is unavailable") from error
    if (
        stat.S_IFMT(metadata.st_mode) != stat.S_IFREG
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or metadata.st_size <= 0
        or metadata.st_size > MAX_SELF_BYTES
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ResponderError("responder source has unsafe metadata")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
        try:
            opened = os.fstat(descriptor)
            chunks: list[bytes] = []
            remaining = MAX_SELF_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(16384, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise ResponderError("responder source is unavailable") from error
    if (
        (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
        != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        or len(data) != metadata.st_size
    ):
        raise ResponderError("responder source changed during verification")
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected_sha256):
        raise ResponderError("responder self hash mismatch")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-self-sha", required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--host-name", type=_pull_host, required=True)
    parser.add_argument("--listen-address", type=lambda value: str(_private_ipv4(value, "listen address")), required=True)
    parser.add_argument("--inherited-fd", type=int, default=0)
    parser.add_argument("--max-cache-age", type=int, default=DEFAULT_MAX_CACHE_AGE_SECONDS)
    args = parser.parse_args(argv)
    if args.inherited_fd < 0 or not 1 <= args.max_cache_age <= 300:
        parser.error("invalid descriptor or cache-age bound")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    listener: Optional[socket.socket] = None
    try:
        verify_self(Path(os.path.abspath(__file__)), args.expected_self_sha)
        descriptor = os.dup(args.inherited_fd)
        listener = socket.socket(fileno=descriptor)
        serve_listener(
            listener,
            args.cache,
            args.key_file,
            args.host_name,
            args.listen_address,
            args.max_cache_age,
        )
        return 0
    except (OSError, ResponderError):
        return 1
    finally:
        if listener is not None:
            listener.close()


if __name__ == "__main__":
    sys.exit(main())
