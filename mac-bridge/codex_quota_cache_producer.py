"""Produce a privacy-minimal imDisplay Codex quota cache from a hook transcript."""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import stat
import sys
import time
from typing import Any, BinaryIO, TextIO


CACHE_PATH = Path.home() / "Library/Caches/imDisplay/codex-budget.json"
TRANSCRIPT_ROOT = Path.home() / ".codex/sessions"
CACHE_NAME = "codex-budget.json"
LOCK_NAME = ".codex-budget.lock"
MAX_CACHE_BYTES = 16 * 1024
MAX_SELF_BYTES = 128 * 1024
MAX_HOOK_INPUT_BYTES = 256 * 1024
MAX_TRANSCRIPT_TAIL_BYTES = 2 * 1024 * 1024
MAX_TRANSCRIPT_LINE_BYTES = 256 * 1024
POST_TOOL_COALESCE_SECONDS = 60
MAX_FUTURE_SKEW_SECONDS = 5
MAX_EPOCH_SECONDS = 4_102_444_800
EXPECTED_TOP_LEVEL_KEYS = {"ordinal", "payload", "timestamp", "type"}
EXPECTED_PAYLOAD_KEYS = {"info", "rate_limits", "type"}
ALLOWED_RATE_LIMIT_KEYS = {
    "credits",
    "individual_limit",
    "limit_id",
    "limit_name",
    "plan_type",
    "primary",
    "rate_limit_reached_type",
    "secondary",
    "spend_control_reached",
}
EXPECTED_WINDOW_KEYS = {"resets_at", "used_percent", "window_minutes"}
ALLOWED_CREDIT_KEYS = {
    "balance",
    "has_credits",
    "overage_limit_reached",
    "unlimited",
}


class ProducerError(Exception):
    """A fail-closed producer condition."""


class LockBusy(ProducerError):
    """Another producer owns the cache lock."""


def _reject_constant(value: str) -> None:
    raise ProducerError(f"invalid JSON constant: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProducerError("duplicate JSON key")
        result[key] = value
    return result


def strict_json_loads(raw: bytes) -> Any:
    if not isinstance(raw, bytes):
        raise ProducerError("JSON input must be bytes")
    try:
        text = raw.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProducerError("invalid JSON") from error


def _is_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _bounded_ascii(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ProducerError(f"invalid {label}")
    if not value.isascii() or any(ord(character) < 0x20 for character in value):
        raise ProducerError(f"invalid {label}")
    return value


def _parse_timestamp(value: Any) -> int:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ProducerError("invalid event timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ProducerError("invalid event timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProducerError("event timestamp lacks a timezone")
    epoch = int(parsed.timestamp())
    if not 0 < epoch <= MAX_EPOCH_SECONDS:
        raise ProducerError("event timestamp is out of range")
    return epoch


def _validate_ignored_rate_limit_metadata(rate_limits: dict[str, Any]) -> str:
    limit_name = rate_limits.get("limit_name")
    display_name = "Codex"
    if limit_name is not None:
        display_name = _bounded_ascii(limit_name, "limit name", 31)
    if rate_limits.get("individual_limit") is not None:
        raise ProducerError("unsupported individual-limit metadata")
    reached_type = rate_limits.get("rate_limit_reached_type")
    if reached_type is not None:
        _bounded_ascii(reached_type, "rate-limit reached type", 64)
    spend_control_reached = rate_limits.get("spend_control_reached")
    if spend_control_reached is not None and not isinstance(spend_control_reached, bool):
        raise ProducerError("invalid spend-control metadata")
    plan_type = rate_limits.get("plan_type")
    if plan_type is not None:
        _bounded_ascii(plan_type, "plan type", 64)
    credits = rate_limits.get("credits")
    if credits is None:
        return display_name
    if not isinstance(credits, dict) or not set(credits).issubset(ALLOWED_CREDIT_KEYS):
        raise ProducerError("invalid credits metadata")
    for key, value in credits.items():
        if key == "balance":
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                raise ProducerError("invalid credits balance metadata")
            if isinstance(value, str) and len(value) > 64:
                raise ProducerError("invalid credits balance metadata")
            if isinstance(value, float) and not math.isfinite(value):
                raise ProducerError("invalid credits balance metadata")
        elif not isinstance(value, bool):
            raise ProducerError("invalid credits flag metadata")
    return display_name


def _window_from_source(name: str, raw: Any, limit_name: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != EXPECTED_WINDOW_KEYS:
        raise ProducerError("invalid rate-limit window schema")
    used = raw["used_percent"]
    if not _is_number(used) or not 0 <= float(used) <= 100:
        raise ProducerError("invalid used percentage")
    minutes = raw["window_minutes"]
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise ProducerError("invalid rate-limit duration")
    if not 0 < minutes <= 525_600:
        raise ProducerError("invalid rate-limit duration")
    resets_at = raw["resets_at"]
    if resets_at is not None and (
        isinstance(resets_at, bool)
        or not isinstance(resets_at, int)
        or not 0 < resets_at <= MAX_EPOCH_SECONDS
    ):
        raise ProducerError("invalid reset timestamp")
    used_percent = round(float(used), 1)
    return {
        "limitId": "codex",
        "limitName": limit_name,
        "remainingPercent": round(100.0 - used_percent, 1),
        "resetsAt": resets_at,
        "usedPercent": used_percent,
        "window": name,
    }


def cache_from_event(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict) or set(event) != EXPECTED_TOP_LEVEL_KEYS:
        raise ProducerError("invalid token-count event schema")
    if event["type"] != "event_msg":
        raise ProducerError("not an event message")
    ordinal = event["ordinal"]
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
        raise ProducerError("invalid event ordinal")
    payload = event["payload"]
    if not isinstance(payload, dict) or set(payload) != EXPECTED_PAYLOAD_KEYS:
        raise ProducerError("invalid token-count payload schema")
    if payload["type"] != "token_count" or not isinstance(payload["info"], dict):
        raise ProducerError("not a valid token-count payload")
    rate_limits = payload["rate_limits"]
    if not isinstance(rate_limits, dict):
        raise ProducerError("rate limits are unavailable")
    if not set(rate_limits).issubset(ALLOWED_RATE_LIMIT_KEYS):
        raise ProducerError("unknown rate-limit schema")
    if rate_limits.get("limit_id") != "codex" or "primary" not in rate_limits:
        raise ProducerError("unsupported rate-limit identity")
    limit_name = _validate_ignored_rate_limit_metadata(rate_limits)
    windows = []
    for name in ("primary", "secondary"):
        parsed = _window_from_source(name, rate_limits.get(name), limit_name)
        if parsed is not None:
            windows.append(parsed)
    cache = {
        "schemaVersion": 1,
        "checkedAt": _parse_timestamp(event["timestamp"]),
        "windows": windows,
    }
    return validate_cache_schema(cache)


def validate_cache_schema(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"schemaVersion", "checkedAt", "windows"}:
        raise ProducerError("unsupported cache schema")
    if raw["schemaVersion"] != 1:
        raise ProducerError("unsupported cache version")
    checked_at = raw["checkedAt"]
    if (
        isinstance(checked_at, bool)
        or not isinstance(checked_at, int)
        or not 0 < checked_at <= MAX_EPOCH_SECONDS
    ):
        raise ProducerError("invalid cache timestamp")
    windows = raw["windows"]
    if not isinstance(windows, list) or not 1 <= len(windows) <= 6:
        raise ProducerError("invalid cache window count")
    required = {"limitId", "limitName", "remainingPercent", "resetsAt", "window"}
    allowed = required | {"usedPercent"}
    seen: set[tuple[str, str]] = set()
    for item in windows:
        if not isinstance(item, dict) or not required.issubset(item) or not set(item).issubset(allowed):
            raise ProducerError("invalid cache window schema")
        limit_id = _bounded_ascii(item["limitId"], "limit id", 32)
        limit_name = item["limitName"]
        if limit_name is not None:
            _bounded_ascii(limit_name, "limit name", 31)
        window = _bounded_ascii(item["window"], "window name", 19)
        identity = (limit_id, window)
        if identity in seen:
            raise ProducerError("duplicate cache window")
        seen.add(identity)
        remaining = item["remainingPercent"]
        if not _is_number(remaining) or not 0 <= float(remaining) <= 100:
            raise ProducerError("invalid remaining percentage")
        if "usedPercent" in item:
            used = item["usedPercent"]
            if not _is_number(used) or not 0 <= float(used) <= 100:
                raise ProducerError("invalid used percentage")
        resets_at = item["resetsAt"]
        if resets_at is not None and (
            isinstance(resets_at, bool)
            or not isinstance(resets_at, int)
            or not 0 < resets_at <= MAX_EPOCH_SECONDS
        ):
            raise ProducerError("invalid reset timestamp")
    return raw


def serialize_cache(raw: dict[str, Any]) -> bytes:
    validated = validate_cache_schema(raw)
    encoded = (
        json.dumps(validated, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")
    if len(encoded) > MAX_CACHE_BYTES:
        raise ProducerError("cache exceeds size limit")
    return encoded


def _open_valid_transcript(path_value: str, root: Path) -> tuple[int, os.stat_result]:
    path = Path(path_value)
    if not path.is_absolute() or path.suffix != ".jsonl":
        raise ProducerError("invalid transcript path")
    if os.path.normpath(path_value) != path_value:
        raise ProducerError("transcript path is not canonical")
    try:
        root_resolved = root.resolve(strict=True)
        path_resolved = path.resolve(strict=True)
    except OSError as error:
        raise ProducerError("transcript path is unavailable") from error
    try:
        common = os.path.commonpath((str(root_resolved), str(path_resolved)))
        relative = path.relative_to(root)
    except ValueError as error:
        raise ProducerError("invalid transcript path") from error
    if common != str(root_resolved) or path == root_resolved:
        raise ProducerError("transcript path is outside the session root")
    try:
        root_metadata = os.lstat(root)
    except OSError as error:
        raise ProducerError("transcript root is unavailable") from error
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(root_metadata.st_mode) & 0o022
    ):
        raise ProducerError("unsafe transcript root")
    current = root
    for component in relative.parts:
        current = current / component
        try:
            component_metadata = os.lstat(current)
            if stat.S_ISLNK(component_metadata.st_mode):
                raise ProducerError("transcript path contains a symlink")
            if current != path and (
                not stat.S_ISDIR(component_metadata.st_mode)
                or component_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(component_metadata.st_mode) & 0o022
            ):
                raise ProducerError("unsafe transcript directory")
        except OSError as error:
            raise ProducerError("transcript path is unavailable") from error
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ProducerError("cannot open transcript") from error
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        os.close(descriptor)
        raise ProducerError("unsafe transcript identity")
    return descriptor, metadata


def _read_bounded_tail(descriptor: int, size: int, maximum: int) -> bytes:
    if maximum <= 0:
        raise ProducerError("invalid tail bound")
    offset = max(0, size - maximum)
    os.lseek(descriptor, offset, os.SEEK_SET)
    remaining = maximum
    chunks: list[bytes] = []
    while remaining:
        chunk = os.read(descriptor, min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if offset:
        newline = data.find(b"\n")
        data = b"" if newline < 0 else data[newline + 1 :]
    return data


def _newest_valid_cache_from_tail(data: bytes) -> dict[str, Any]:
    for line in reversed(data.splitlines()):
        if not line or len(line) > MAX_TRANSCRIPT_LINE_BYTES:
            continue
        try:
            event = strict_json_loads(line)
        except ProducerError:
            continue
        if not isinstance(event, dict) or event.get("type") != "event_msg":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        if "rate_limits" not in payload:
            continue
        try:
            return cache_from_event(event)
        except ProducerError:
            continue
    raise ProducerError("no valid rate-limit event in bounded transcript tail")


def _validate_cache_directory(cache_path: Path) -> int:
    if cache_path.name != CACHE_NAME or not cache_path.is_absolute():
        raise ProducerError("invalid cache path")
    directory = cache_path.parent
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise ProducerError("cannot create cache directory") from error
    try:
        metadata = os.lstat(directory)
    except OSError as error:
        raise ProducerError("cache directory is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ProducerError("unsafe cache directory")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError as error:
        raise ProducerError("cannot open cache directory") from error
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
        os.close(descriptor)
        raise ProducerError("cache directory changed during validation")
    return descriptor


def _open_lock(directory_descriptor: int) -> int:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(LOCK_NAME, flags, 0o600, dir_fd=directory_descriptor)
    except OSError as error:
        raise ProducerError("cannot open cache lock") from error
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise ProducerError("unsafe cache lock")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(descriptor)
        raise LockBusy("cache lock is busy") from error
    return descriptor


def _read_all(descriptor: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(4096, maximum + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum:
            raise ProducerError("file exceeds size limit")
    return b"".join(chunks)


def _read_existing_cache(
    directory_descriptor: int,
) -> tuple[dict[str, Any], os.stat_result, bytes] | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(CACHE_NAME, flags, dir_fd=directory_descriptor)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ProducerError("cannot open existing cache") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > MAX_CACHE_BYTES
        ):
            raise ProducerError("unsafe existing cache")
        raw = _read_all(descriptor, MAX_CACHE_BYTES)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or len(raw) != metadata.st_size
    ):
        raise ProducerError("existing cache changed during read")
    parsed = validate_cache_schema(strict_json_loads(raw))
    return parsed, metadata, raw


def _atomic_write_cache(directory_descriptor: int, encoded: bytes) -> None:
    temporary = f".{CACHE_NAME}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    descriptor = -1
    created = False
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_descriptor)
        created = True
        written = 0
        while written < len(encoded):
            count = os.write(descriptor, encoded[written:])
            if count <= 0:
                raise ProducerError("short cache write")
            written += count
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary,
            CACHE_NAME,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        created = False
        os.fsync(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if created:
            try:
                os.unlink(temporary, dir_fd=directory_descriptor)
            except OSError:
                pass


def produce_cache(
    transcript_path: str,
    event_kind: str,
    *,
    cache_path: Path = CACHE_PATH,
    transcript_root: Path = TRANSCRIPT_ROOT,
    now: int | None = None,
    tail_bytes: int = MAX_TRANSCRIPT_TAIL_BYTES,
) -> bool:
    if event_kind not in {"post-tool-use", "stop"}:
        raise ProducerError("unsupported hook event")
    transcript_descriptor, transcript_metadata = _open_valid_transcript(
        transcript_path, transcript_root
    )
    directory_descriptor = -1
    lock_descriptor = -1
    try:
        directory_descriptor = _validate_cache_directory(cache_path)
        lock_descriptor = _open_lock(directory_descriptor)
        existing = _read_existing_cache(directory_descriptor)
        current_time = int(time.time()) if now is None else int(now)
        if existing is not None and existing[0]["checkedAt"] > current_time + MAX_FUTURE_SKEW_SECONDS:
            raise ProducerError("existing cache timestamp is from the future")
        if event_kind == "post-tool-use" and existing is not None:
            metadata = existing[1]
            if metadata.st_mtime > current_time + MAX_FUTURE_SKEW_SECONDS:
                raise ProducerError("cache modification time is from the future")
            if current_time - metadata.st_mtime < POST_TOOL_COALESCE_SECONDS:
                return False
        tail = _read_bounded_tail(
            transcript_descriptor, transcript_metadata.st_size, tail_bytes
        )
        transcript_after = os.fstat(transcript_descriptor)
        if (
            (
                transcript_metadata.st_dev,
                transcript_metadata.st_ino,
                transcript_metadata.st_size,
                transcript_metadata.st_mtime_ns,
            )
            != (
                transcript_after.st_dev,
                transcript_after.st_ino,
                transcript_after.st_size,
                transcript_after.st_mtime_ns,
            )
        ):
            raise ProducerError("transcript changed during read")
        candidate = _newest_valid_cache_from_tail(tail)
        if candidate["checkedAt"] > current_time + MAX_FUTURE_SKEW_SECONDS:
            raise ProducerError("candidate cache timestamp is from the future")
        if existing is not None and candidate["checkedAt"] <= existing[0]["checkedAt"]:
            return False
        encoded = serialize_cache(candidate)
        latest = _read_existing_cache(directory_descriptor)
        if latest is not None and candidate["checkedAt"] <= latest[0]["checkedAt"]:
            return False
        _atomic_write_cache(directory_descriptor, encoded)
        installed = _read_existing_cache(directory_descriptor)
        if installed is None or installed[2] != encoded:
            raise ProducerError("cache readback failed")
        return True
    finally:
        if lock_descriptor >= 0:
            os.close(lock_descriptor)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)
        os.close(transcript_descriptor)


def _verify_self(path: Path, expected_sha: str) -> None:
    if len(expected_sha) != 64 or any(character not in "0123456789abcdef" for character in expected_sha):
        raise ProducerError("invalid expected self hash")
    metadata = os.lstat(path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or not 0 < metadata.st_size <= MAX_SELF_BYTES
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ProducerError("unsafe producer identity")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        raw = _read_all(descriptor, MAX_SELF_BYTES)
    finally:
        os.close(descriptor)
    if (
        (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
        != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        or len(raw) != metadata.st_size
    ):
        raise ProducerError("producer changed during verification")
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha:
        raise ProducerError("producer identity mismatch")


def _read_hook_input(stream: BinaryIO) -> bytes:
    raw = stream.read(MAX_HOOK_INPUT_BYTES + 1)
    if len(raw) > MAX_HOOK_INPUT_BYTES:
        raise ProducerError("hook input exceeds size limit")
    return raw


def _run_hook(
    raw: bytes,
    event_kind: str,
    *,
    expected_self_sha: str,
    script_path: Path,
    cache_path: Path,
    transcript_root: Path,
) -> None:
    _verify_self(script_path, expected_self_sha)
    event = strict_json_loads(raw)
    expected_name = "PostToolUse" if event_kind == "post-tool-use" else "Stop"
    if not isinstance(event, dict) or event.get("hook_event_name") != expected_name:
        raise ProducerError("hook event mismatch")
    transcript_path = event.get("transcript_path")
    if not isinstance(transcript_path, str):
        raise ProducerError("hook transcript path is unavailable")
    produce_cache(
        transcript_path,
        event_kind,
        cache_path=cache_path,
        transcript_root=transcript_root,
    )


def main(
    arguments: list[str] | None = None,
    *,
    stdin: BinaryIO | None = None,
    stdout: TextIO | None = None,
    script_path: Path | None = None,
    cache_path: Path = CACHE_PATH,
    transcript_root: Path = TRANSCRIPT_ROOT,
) -> int:
    args = list(sys.argv[1:] if arguments is None else arguments)
    input_stream = sys.stdin.buffer if stdin is None else stdin
    output_stream = sys.stdout if stdout is None else stdout
    event_kind = args[-1] if len(args) >= 1 else ""
    stop_output = event_kind == "stop"
    try:
        if (
            len(args) != 5
            or args[0] != "--expected-self-sha"
            or args[2:4] != ["hook", "--event"]
            or event_kind not in {"post-tool-use", "stop"}
        ):
            raise ProducerError("invalid invocation")
        raw = _read_hook_input(input_stream)
        _run_hook(
            raw,
            event_kind,
            expected_self_sha=args[1],
            script_path=Path(__file__) if script_path is None else script_path,
            cache_path=cache_path,
            transcript_root=transcript_root,
        )
    except (OSError, ProducerError, ValueError):
        pass
    if stop_output:
        output_stream.write("{}\n")
        output_stream.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
