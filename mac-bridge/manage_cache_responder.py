#!/usr/bin/python3
"""Prepare, stage, check, or recoverably disable the imDisplay cache responder."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import plistlib
import re
import secrets
import stat
import sys
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape as xml_escape


LABEL = "local.imdisplay.cache-responder"
VERSION = "v3"
DEFAULT_PORT = 47832
DEFAULT_CACHE_PATH = Path.home() / "Library/Caches/imDisplay/codex-budget.json"
DEFAULT_ROOT = Path.home() / "Library/Application Support/imDisplay/cache-responder"
DEFAULT_RESPONDER_PATH = DEFAULT_ROOT / VERSION / "imdisplay_cache_responder.py"
PREVIOUS_RESPONDER_PATH = DEFAULT_ROOT / "v2" / "imdisplay_cache_responder.py"
DEFAULT_KEY_PATH = DEFAULT_ROOT / "read-only-key-v1"
DEFAULT_PLIST_PATH = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
DEFAULT_STAGED_PLIST_PATH = DEFAULT_PLIST_PATH.with_name(
    f"{DEFAULT_PLIST_PATH.name}.v3-staged"
)
DEFAULT_PREVIOUS_DISABLED_PLIST_PATH = DEFAULT_PLIST_PATH.with_name(
    f"{DEFAULT_PLIST_PATH.name}.v2-disabled"
)
RESPONDER_NAME = "imdisplay_cache_responder.py"
TEMPLATE_NAME = f"{LABEL}.plist.template"
PROVISIONING_KIND = "budget_pull_config"
MAX_PACKAGE_FILE_BYTES = 128 * 1024
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
LOCAL_HOSTNAME = re.compile(r"(?=.{7,63}\Z)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.local\Z")


class PackageError(Exception):
    """The package or requested lifecycle operation failed validation."""


def private_ipv4(value: str, label: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise PackageError(f"invalid {label}") from error
    if not isinstance(address, ipaddress.IPv4Address) or not any(
        address in network for network in PRIVATE_NETWORKS
    ):
        raise PackageError(f"{label} must be a private IPv4 address")
    return str(address)


def pull_host(value: str) -> str:
    if LOCAL_HOSTNAME.fullmatch(value):
        return value
    try:
        return private_ipv4(value, "pull host")
    except PackageError as error:
        raise PackageError(
            "pull host must be a strict lowercase .local name or private IPv4"
        ) from error


def validate_port(value: int) -> int:
    if not 1024 <= value <= 65535:
        raise PackageError("port must be 1024..65535")
    return value


def _read_bounded_regular(
    path: Path, maximum_bytes: int, label: str, exact_mode: Optional[int] = None
) -> tuple[bytes, os.stat_result]:
    try:
        before = os.lstat(path)
    except OSError as error:
        raise PackageError(f"unable to read {label}") from error
    mode = stat.S_IMODE(before.st_mode)
    if (
        stat.S_IFMT(before.st_mode) != stat.S_IFREG
        or before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or before.st_size <= 0
        or before.st_size > maximum_bytes
        or (mode != exact_mode if exact_mode is not None else bool(mode & 0o022))
    ):
        raise PackageError(f"unsafe {label} metadata")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
        try:
            opened = os.fstat(descriptor)
            chunks: list[bytes] = []
            remaining = maximum_bytes + 1
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
        raise PackageError(f"unable to read {label}") from error
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        or len(data) != before.st_size
    ):
        raise PackageError(f"{label} changed during read")
    return data, before


def sha256_file(path: Path, exact_mode: Optional[int] = None) -> str:
    data, _ = _read_bounded_regular(
        path, MAX_PACKAGE_FILE_BYTES, path.name, exact_mode=exact_mode
    )
    return hashlib.sha256(data).hexdigest()


def validate_key_file(path: Path) -> str:
    try:
        parent = os.lstat(path.parent)
    except OSError as error:
        raise PackageError("read-only key is unavailable") from error
    if (
        stat.S_IFMT(parent.st_mode) != stat.S_IFDIR
        or parent.st_uid != os.geteuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
    ):
        raise PackageError("read-only key metadata is not owner-only")
    data, _ = _read_bounded_regular(path, 65, "read-only key", exact_mode=0o600)
    encoded = data[:-1] if data.endswith(b"\n") else data
    if len(encoded) != 64:
        raise PackageError("read-only key must contain 32 lowercase-hex bytes")
    try:
        value = encoded.decode("ascii")
        decoded = bytes.fromhex(value)
    except (UnicodeDecodeError, ValueError) as error:
        raise PackageError("invalid read-only key") from error
    if value != value.lower() or len(decoded) != 32:
        raise PackageError("read-only key must contain 32 lowercase-hex bytes")
    return value


def _validate_directory(path: Path, exact_mode: bool) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise PackageError(f"directory is unavailable: {path}") from error
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        stat.S_IFMT(metadata.st_mode) != stat.S_IFDIR
        or metadata.st_uid != os.geteuid()
        or (mode != 0o700 if exact_mode else bool(mode & 0o022))
    ):
        raise PackageError(f"directory metadata is unsafe: {path}")


def _ensure_private_directory(path: Path) -> None:
    if path.exists():
        _validate_directory(path, exact_mode=True)
        return
    parent = path.parent
    if not parent.exists():
        _ensure_private_directory(parent)
    else:
        _validate_directory(parent, exact_mode=False)
    try:
        os.mkdir(path, 0o700)
    except OSError as error:
        raise PackageError(f"unable to create private directory: {path}") from error
    _validate_directory(path, exact_mode=True)


def _atomic_write(path: Path, data: bytes, mode: int, replace: bool) -> None:
    if replace:
        raise PackageError("atomic replacement is forbidden")
    _validate_directory(path.parent, exact_mode=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(str(temporary), flags, mode)
    try:
        try:
            written = 0
            while written < len(data):
                count = os.write(descriptor, data[written:])
                if count <= 0:
                    raise PackageError(f"short write for {path}")
                written += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(temporary, mode)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as error:
            raise PackageError(f"refusing to overwrite {path}") from error
        temporary.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()


def render_plist(
    template: bytes,
    responder_path: Path,
    responder_sha256: str,
    cache_path: Path,
    key_path: Path,
    host_name: str,
    listen_address: str,
    port: int,
) -> bytes:
    replacements = {
        "__RESPONDER_PATH__": str(responder_path),
        "__RESPONDER_SHA256__": responder_sha256,
        "__CACHE_PATH__": str(cache_path),
        "__KEY_PATH__": str(key_path),
        "__HOST_NAME__": pull_host(host_name),
        "__LISTEN_ADDRESS__": private_ipv4(listen_address, "listen address"),
        "__PORT__": str(validate_port(port)),
    }
    try:
        text = template.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PackageError("plist template is not UTF-8") from error
    expected_counts = {token: 1 for token in replacements}
    expected_counts["__LISTEN_ADDRESS__"] = 2
    for token, value in replacements.items():
        if text.count(token) != expected_counts[token]:
            raise PackageError(f"plist template token mismatch: {token}")
        text = text.replace(token, xml_escape(value))
    if re.search(r"__[A-Z0-9_]+__", text):
        raise PackageError("unresolved plist template token")
    rendered = text.encode("utf-8")
    try:
        payload = plistlib.loads(rendered)
    except Exception as error:
        raise PackageError("rendered plist is invalid") from error
    forbidden = {"RunAtLoad", "KeepAlive", "StartInterval", "StartCalendarInterval"}
    if forbidden.intersection(payload) or payload.get("Label") != LABEL:
        raise PackageError("rendered plist violates the socket-only lifecycle")
    arguments = payload.get("ProgramArguments")
    expected_arguments = [
        "/usr/bin/python3",
        "-I",
        "-S",
        str(responder_path),
        "--expected-self-sha",
        responder_sha256,
        "--cache",
        str(cache_path),
        "--key-file",
        str(key_path),
        "--host-name",
        replacements["__HOST_NAME__"],
        "--listen-address",
        replacements["__LISTEN_ADDRESS__"],
        "--inherited-fd",
        "0",
        "--max-cache-age",
        "150",
    ]
    if arguments != expected_arguments:
        raise PackageError("rendered plist has unexpected program arguments")
    if payload.get("inetdCompatibility") != {"Wait": True}:
        raise PackageError("rendered plist must pass one bounded listening socket")
    if payload.get("ThrottleInterval") != 10:
        raise PackageError("rendered plist must throttle relaunches")
    sockets = payload.get("Sockets")
    quota_socket = sockets.get("QuotaHTTP") if isinstance(sockets, dict) else None
    if (
        not isinstance(quota_socket, dict)
        or quota_socket.get("SockFamily") != "IPv4"
        or quota_socket.get("SockType") != "stream"
        or quota_socket.get("SockProtocol") != "TCP"
        or quota_socket.get("SockServiceName") != port
        or quota_socket.get("SockNodeName") != replacements["__LISTEN_ADDRESS__"]
    ):
        raise PackageError("rendered plist has an invalid launchd socket")
    return rendered


def package_paths(package_dir: Path) -> tuple[Path, Path]:
    return package_dir / RESPONDER_NAME, package_dir / TEMPLATE_NAME


def check_package(package_dir: Path) -> dict[str, object]:
    responder, template = package_paths(package_dir)
    responder_sha256 = sha256_file(responder)
    template_bytes, _ = _read_bounded_regular(
        template, MAX_PACKAGE_FILE_BYTES, "plist template"
    )
    rendered = render_plist(
        template_bytes,
        DEFAULT_RESPONDER_PATH,
        responder_sha256,
        DEFAULT_CACHE_PATH,
        DEFAULT_KEY_PATH,
        "imdisplay-mac.local",
        "192.168.1.10",
        DEFAULT_PORT,
    )
    return {
        "status": "PREPARED",
        "protocol": 1,
        "responderSha256": responder_sha256,
        "renderedPlistBytes": len(rendered),
        "versionedResponder": str(DEFAULT_RESPONDER_PATH),
        "launchAgent": str(DEFAULT_PLIST_PATH),
        "stagedLaunchAgent": str(DEFAULT_STAGED_PLIST_PATH),
        "activated": False,
    }


def prepare_key(key_path: Path, provisioning_output: Path, mac_host: str, port: int) -> None:
    if key_path.exists() or provisioning_output.exists():
        raise PackageError("refusing to overwrite key or provisioning material")
    _ensure_private_directory(key_path.parent)
    _validate_directory(provisioning_output.parent, exact_mode=True)
    key_hex = secrets.token_hex(32)
    _atomic_write(key_path, f"{key_hex}\n".encode("ascii"), 0o600, replace=False)
    payload = {
        "schema": 1,
        "kind": PROVISIONING_KIND,
        "enabled": True,
        "macHost": pull_host(mac_host),
        "macPort": validate_port(port),
        "readOnlyKey": key_hex,
        "legacyPushEnabled": False,
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    try:
        _atomic_write(provisioning_output, encoded + b"\n", 0o600, replace=False)
    except Exception:
        key_path.unlink(missing_ok=True)
        raise


def install(
    package_dir: Path,
    responder_path: Path,
    plist_path: Path,
    cache_path: Path,
    key_path: Path,
    host_name: str,
    listen_address: str,
    port: int,
    legacy_plist: Optional[Path] = None,
) -> str:
    if legacy_plist is not None and legacy_plist.exists():
        raise PackageError("legacy push plist must first be booted out and recoverably disabled")
    if plist_path.exists():
        raise PackageError("refusing to replace an existing responder plist")
    validate_key_file(key_path)
    responder, template = package_paths(package_dir)
    source, _ = _read_bounded_regular(
        responder, MAX_PACKAGE_FILE_BYTES, "responder package"
    )
    template_bytes, _ = _read_bounded_regular(
        template, MAX_PACKAGE_FILE_BYTES, "plist template"
    )
    responder_sha256 = hashlib.sha256(source).hexdigest()
    rendered = render_plist(
        template_bytes,
        responder_path,
        responder_sha256,
        cache_path,
        key_path,
        host_name,
        listen_address,
        port,
    )
    if responder_path.exists():
        metadata = os.lstat(responder_path)
        if (
            stat.S_IFMT(metadata.st_mode) != stat.S_IFREG
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o500
            or sha256_file(responder_path, exact_mode=0o500) != responder_sha256
        ):
            raise PackageError("versioned responder path already contains different code")
    else:
        _ensure_private_directory(responder_path.parent)
        _atomic_write(responder_path, source, 0o500, replace=False)
    _validate_directory(plist_path.parent, exact_mode=True)
    _atomic_write(plist_path, rendered, 0o600, replace=False)
    return responder_sha256


def validate_previous_plist(
    active: bytes,
    previous_responder_path: Path,
    previous_sha256: str,
    cache_path: Path,
    key_path: Path,
    host_name: str,
    port: int,
) -> None:
    try:
        payload = plistlib.loads(active)
    except Exception as error:
        raise PackageError("active previous plist is invalid") from error
    label = payload.get("Label")
    if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9.-]{1,128}", label):
        raise PackageError("active previous plist has an invalid label")
    forbidden = {"RunAtLoad", "KeepAlive", "StartInterval", "StartCalendarInterval"}
    expected_arguments = [
        "/usr/bin/python3",
        str(previous_responder_path),
        "--expected-self-sha",
        previous_sha256,
        "--cache",
        str(cache_path),
        "--key-file",
        str(key_path),
        "--host-name",
        pull_host(host_name),
        "--inherited-fd",
        "0",
        "--max-cache-age",
        "150",
    ]
    sockets = payload.get("Sockets")
    quota_socket = sockets.get("QuotaHTTP") if isinstance(sockets, dict) else None
    if (
        forbidden.intersection(payload)
        or payload.get("ProgramArguments") != expected_arguments
        or payload.get("inetdCompatibility") != {"Wait": False}
        or payload.get("ProcessType") != "Background"
        or payload.get("Umask") != 63
        or payload.get("HardResourceLimits")
        != {"Core": 0, "CPU": 5, "NumberOfFiles": 32}
        or not isinstance(quota_socket, dict)
        or quota_socket.get("SockFamily") != "IPv4"
        or quota_socket.get("SockType") != "stream"
        or quota_socket.get("SockProtocol") != "TCP"
        or quota_socket.get("SockServiceName") != validate_port(port)
        or "SockNodeName" in quota_socket
    ):
        raise PackageError("active previous plist does not match the immutable package")


def stage_upgrade(
    package_dir: Path,
    responder_path: Path,
    staged_plist_path: Path,
    active_plist_path: Path,
    previous_responder_path: Path,
    disabled_previous_plist_path: Path,
    cache_path: Path,
    key_path: Path,
    host_name: str,
    listen_address: str,
    port: int,
    legacy_plist: Optional[Path] = None,
) -> str:
    if disabled_previous_plist_path.exists():
        raise PackageError("previous disabled plist target already exists")
    previous_sha256 = sha256_file(previous_responder_path, exact_mode=0o500)
    active, _ = _read_bounded_regular(
        active_plist_path,
        MAX_PACKAGE_FILE_BYTES,
        "active previous plist",
        exact_mode=0o600,
    )
    validate_previous_plist(
        active,
        previous_responder_path,
        previous_sha256,
        cache_path,
        key_path,
        host_name,
        port,
    )
    return install(
        package_dir,
        responder_path,
        staged_plist_path,
        cache_path,
        key_path,
        host_name,
        listen_address,
        port,
        legacy_plist=legacy_plist,
    )


def rollback(plist_path: Path) -> Path:
    disabled = plist_path.with_name(f"{plist_path.name}.disabled")
    _validate_directory(plist_path.parent, exact_mode=True)
    try:
        metadata = os.lstat(plist_path)
    except OSError as error:
        raise PackageError("installed responder plist is absent") from error
    if (
        stat.S_IFMT(metadata.st_mode) != stat.S_IFREG
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise PackageError("installed responder plist metadata is unsafe")
    if disabled.exists():
        raise PackageError("disabled responder plist already exists")
    linked = False
    try:
        os.link(plist_path, disabled, follow_symlinks=False)
        linked = True
        plist_path.unlink()
    except OSError as error:
        if linked and disabled.exists() and plist_path.exists():
            disabled.unlink()
        raise PackageError("unable to recoverably disable responder plist") from error
    return disabled


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("check-only")

    prepare = actions.add_parser("prepare-key")
    prepare.add_argument("--provisioning-output", type=Path, required=True)
    prepare.add_argument("--key-file", type=Path, default=DEFAULT_KEY_PATH)
    prepare.add_argument("--mac-host", type=pull_host, required=True)
    prepare.add_argument("--port", type=validate_port, default=DEFAULT_PORT)

    stage = actions.add_parser("install")
    stage.add_argument("--mac-host", type=pull_host, required=True)
    stage.add_argument("--listen-address", type=lambda value: private_ipv4(value, "listen address"), required=True)
    stage.add_argument("--port", type=validate_port, default=DEFAULT_PORT)
    stage.add_argument("--responder-path", type=Path, default=DEFAULT_RESPONDER_PATH)
    stage.add_argument("--plist-path", type=Path, default=DEFAULT_PLIST_PATH)
    stage.add_argument("--cache-path", type=Path, default=DEFAULT_CACHE_PATH)
    stage.add_argument("--key-file", type=Path, default=DEFAULT_KEY_PATH)
    stage.add_argument("--legacy-plist", type=Path)

    upgrade = actions.add_parser("stage-upgrade")
    upgrade.add_argument("--mac-host", type=pull_host, required=True)
    upgrade.add_argument("--listen-address", type=lambda value: private_ipv4(value, "listen address"), required=True)
    upgrade.add_argument("--port", type=validate_port, default=DEFAULT_PORT)
    upgrade.add_argument("--responder-path", type=Path, default=DEFAULT_RESPONDER_PATH)
    upgrade.add_argument("--staged-plist", type=Path, default=DEFAULT_STAGED_PLIST_PATH)
    upgrade.add_argument("--active-plist", type=Path, default=DEFAULT_PLIST_PATH)
    upgrade.add_argument("--previous-responder", type=Path, default=PREVIOUS_RESPONDER_PATH)
    upgrade.add_argument(
        "--disabled-previous-plist",
        type=Path,
        default=DEFAULT_PREVIOUS_DISABLED_PLIST_PATH,
    )
    upgrade.add_argument("--cache-path", type=Path, default=DEFAULT_CACHE_PATH)
    upgrade.add_argument("--key-file", type=Path, default=DEFAULT_KEY_PATH)
    upgrade.add_argument("--legacy-plist", type=Path)

    rollback_action = actions.add_parser("rollback")
    rollback_action.add_argument("--plist-path", type=Path, default=DEFAULT_PLIST_PATH)
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    package_dir = Path(os.path.abspath(__file__)).parent
    try:
        if args.action == "check-only":
            print(json.dumps(check_package(package_dir), sort_keys=True))
        elif args.action == "prepare-key":
            prepare_key(args.key_file, args.provisioning_output, args.mac_host, args.port)
            print(json.dumps({"status": "PREPARED", "keyFile": str(args.key_file),
                              "provisioningFile": str(args.provisioning_output)}, sort_keys=True))
        elif args.action == "install":
            digest = install(
                package_dir,
                args.responder_path,
                args.plist_path,
                args.cache_path,
                args.key_file,
                args.mac_host,
                args.listen_address,
                args.port,
                legacy_plist=args.legacy_plist,
            )
            print(json.dumps({"status": "STAGED_NOT_BOOTSTRAPPED", "sha256": digest,
                              "plist": str(args.plist_path)}, sort_keys=True))
        elif args.action == "stage-upgrade":
            digest = stage_upgrade(
                package_dir,
                args.responder_path,
                args.staged_plist,
                args.active_plist,
                args.previous_responder,
                args.disabled_previous_plist,
                args.cache_path,
                args.key_file,
                args.mac_host,
                args.listen_address,
                args.port,
                legacy_plist=args.legacy_plist,
            )
            print(json.dumps({
                "status": "V3_STAGED_PREVIOUS_UNCHANGED",
                "sha256": digest,
                "plist": str(args.staged_plist),
                "responder": str(args.responder_path),
            }, sort_keys=True))
        elif args.action == "rollback":
            disabled = rollback(args.plist_path)
            print(json.dumps({"status": "DISABLED", "plist": str(disabled)}, sort_keys=True))
        return 0
    except (OSError, PackageError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
