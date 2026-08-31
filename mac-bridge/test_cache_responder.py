#!/usr/bin/python3

from __future__ import annotations

import ast
import hashlib
import hmac
import importlib.util
import ipaddress
import json
import os
import plistlib
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent


def load_module(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


responder = load_module("imdisplay_cache_responder", ROOT / "imdisplay_cache_responder.py")
manager = load_module("manage_cache_responder", ROOT / "manage_cache_responder.py")


KEY_HEX = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
KEY = bytes.fromhex(KEY_HEX)
NONCE = "0123456789abcdef0123456789abcdef"
OTHER_NONCE = "fedcba9876543210fedcba9876543210"
DEVICE_IP = "192.168.1.50"
MAC_IP = "192.168.1.10"
HOST_NAME = "imdisplay-mac.local"
PORT = 47832


def sample_cache(now: int, remaining: float = 47.5) -> dict:
    return {
        "schemaVersion": 1,
        "checkedAt": now - 10,
        "windows": [
            {
                "limitId": "codex",
                "limitName": "Codex",
                "remainingPercent": remaining,
                "resetsAt": now + 3600,
                "window": "primary",
                "usedPercent": 100 - remaining,
            }
        ],
    }


def write_private(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


def signed_request(host: str = f"{HOST_NAME}:{PORT}", nonce: str = NONCE) -> bytes:
    authorization = hmac.new(KEY, responder.request_canonical(nonce), hashlib.sha256).hexdigest()
    return (
        "GET /v1/quota HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Connection: close\r\n"
        "X-imDisplay-Protocol: 1\r\n"
        f"X-imDisplay-Nonce: {nonce}\r\n"
        f"X-imDisplay-Authorization: {authorization}\r\n\r\n"
    ).encode("ascii")


def parse_signed_response(data: bytes, expected_nonce: str) -> dict:
    headers, body = data.split(b"\r\n\r\n", 1)
    lines = headers.decode("ascii").split("\r\n")
    if lines[0] != "HTTP/1.1 200 OK":
        raise AssertionError("unexpected status")
    values = dict(line.split(": ", 1) for line in lines[1:])
    if values["X-imDisplay-Nonce"] != expected_nonce:
        raise AssertionError("response replay nonce")
    body_hash = hashlib.sha256(body).hexdigest()
    if values["X-imDisplay-Body-SHA256"] != body_hash:
        raise AssertionError("body hash")
    expected_auth = hmac.new(
        KEY, responder.response_canonical(expected_nonce, body_hash), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(values["X-imDisplay-Authorization"], expected_auth):
        raise AssertionError("response authentication")
    if int(values["Content-Length"]) != len(body):
        raise AssertionError("content length")
    return json.loads(body)


class FakeConnection:
    family = socket.AF_INET
    type = socket.SOCK_STREAM

    def __init__(self, request: bytes, peer: str = DEVICE_IP, local: str = MAC_IP):
        self.chunks = [request]
        self.peer = (peer, 50123)
        self.local = (local, PORT)
        self.sent: list[bytes] = []
        self.timeout = None
        self.recv_calls = 0
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def getsockopt(self, level: int, option: int) -> int:
        self.assert_socket_option(level, option)
        return socket.SOCK_STREAM

    def assert_socket_option(self, level: int, option: int) -> None:
        if (level, option) != (socket.SOL_SOCKET, socket.SO_TYPE):
            raise AssertionError("unexpected socket option")

    def getpeername(self):
        return self.peer

    def getsockname(self):
        return self.local

    def recv(self, count: int) -> bytes:
        self.recv_calls += 1
        return self.chunks.pop(0) if self.chunks else b""

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def close(self) -> None:
        self.closed = True


class FakeListener:
    family = socket.AF_INET
    type = socket.SOCK_STREAM

    def __init__(self, connections: list[FakeConnection], local: str = MAC_IP):
        self.connections = connections
        self.local = (local, PORT)
        self.timeout = None
        self.accept_calls = 0

    def getsockopt(self, level: int, option: int) -> int:
        if (level, option) != (socket.SOL_SOCKET, socket.SO_TYPE):
            raise AssertionError("unexpected socket option")
        return socket.SOCK_STREAM

    def getsockname(self):
        return self.local

    def getpeername(self):
        raise OSError("listener has no peer")

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def accept(self):
        self.accept_calls += 1
        if not self.connections:
            raise socket.timeout("bounded idle")
        connection = self.connections.pop(0)
        return connection, connection.peer


class TimeoutConnection(FakeConnection):
    def recv(self, count: int) -> bytes:
        self.recv_calls += 1
        raise socket.timeout("bounded test timeout")


class CacheValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.cache = self.root / "codex-budget.json"
        self.key = self.root / "read-only-key-v1"
        write_private(self.key, f"{KEY_HEX}\n".encode("ascii"))
        self.now = int(time.time())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_cache(self, value: dict, modified_at: int | None = None) -> None:
        write_private(
            self.cache,
            json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii"),
        )
        timestamp = self.now if modified_at is None else modified_at
        os.utime(self.cache, (timestamp, timestamp))

    def test_fresh_cache_maps_only_compact_quota(self):
        self.write_cache(sample_cache(self.now))
        raw, age = responder.load_quota_cache(self.cache, self.now, 150)
        payload = responder.make_payload(raw, age)
        self.assertEqual(payload["sourceAgeSeconds"], 10)
        self.assertEqual(payload["windows"][0]["remaining"], 47.5)
        self.assertNotIn("usedPercent", json.dumps(payload))
        self.assertLessEqual(len(responder.encode_payload(payload)), 2047)

    def test_cache_identifiers_and_names_do_not_reach_device(self):
        value = sample_cache(self.now)
        value["windows"][0]["limitId"] = "private-account-identifier"
        value["windows"][0]["limitName"] = "Private Account Name"
        value["windows"][0]["window"] = "private-window-id"
        self.write_cache(value)
        raw, age = responder.load_quota_cache(self.cache, self.now, 150)
        encoded = responder.encode_payload(responder.make_payload(raw, age))
        self.assertNotIn(b"private-account", encoded)
        self.assertNotIn(b"Private Account", encoded)
        self.assertNotIn(b"private-window", encoded)
        self.assertIn(b'"id":"quota:1"', encoded)

    def test_missing_invalid_and_stale_are_explicit_without_zero(self):
        missing = responder.cache_payload(self.cache, self.now, 150)
        self.assertEqual((missing["ok"], missing["stale"], missing["windows"]),
                         (False, False, []))
        self.assertNotIn("remaining", json.dumps(missing))
        stale = sample_cache(self.now)
        stale["checkedAt"] = self.now - 151
        self.write_cache(stale)
        payload = responder.cache_payload(self.cache, self.now, 150)
        self.assertEqual(payload["error"], "Stale data")
        self.assertGreater(payload["sourceAgeSeconds"], 150)

    def test_schema_is_exact_bounded_and_duplicate_safe(self):
        invalid = sample_cache(self.now)
        invalid["extra"] = True
        with self.assertRaises(responder.CacheError):
            responder.validate_cache_schema(invalid)
        invalid = sample_cache(self.now)
        invalid["windows"] *= 7
        with self.assertRaises(responder.CacheError):
            responder.validate_cache_schema(invalid)
        with self.assertRaises(responder.CacheError):
            responder.parse_strict_json(b'{"schemaVersion":1,"schemaVersion":1}')
        with self.assertRaises(responder.CacheError):
            responder.parse_strict_json(b'{"value":NaN}')

    def test_owner_type_link_size_and_freshness_fail_closed(self):
        self.write_cache(sample_cache(self.now))
        self.cache.chmod(0o644)
        with self.assertRaises(responder.CacheError):
            responder.load_quota_cache(self.cache, self.now, 150)
        self.cache.unlink()
        write_private(self.cache, b"x" * (responder.MAX_CACHE_BYTES + 1))
        with self.assertRaises(responder.CacheError):
            responder.load_quota_cache(self.cache, self.now, 150)
        self.cache.unlink()
        target = self.root / "target"
        write_private(target, b"{}")
        self.cache.symlink_to(target)
        with self.assertRaises(responder.CacheError):
            responder.load_quota_cache(self.cache, self.now, 150)

    def test_future_cache_and_bad_key_fail_closed(self):
        future = sample_cache(self.now)
        future["checkedAt"] = self.now + 31
        self.write_cache(future)
        with self.assertRaises(responder.CacheError):
            responder.load_quota_cache(self.cache, self.now, 150)
        write_private(self.key, b"A" * 64)
        with self.assertRaises(responder.CacheError):
            responder.load_key(self.key)


class AuthenticationAndActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.cache = self.root / "codex-budget.json"
        self.key = self.root / "read-only-key-v1"
        write_private(self.key, f"{KEY_HEX}\n".encode("ascii"))
        self.now = int(time.time())
        write_private(
            self.cache,
            json.dumps(sample_cache(self.now), separators=(",", ":")).encode("ascii"),
        )
        os.utime(self.cache, (self.now, self.now))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_request_requires_exact_host_hmac_nonce_and_headers(self):
        self.assertEqual(responder.parse_request(signed_request(), KEY,
                                                 f"{HOST_NAME}:{PORT}"), NONCE)
        with self.assertRaises(responder.RequestError):
            responder.parse_request(signed_request("192.168.1.11:47832"), KEY,
                                    f"{HOST_NAME}:{PORT}")
        damaged = signed_request().replace(b"X-imDisplay-Authorization: ",
                                           b"X-imDisplay-Authorization: 0", 1)
        with self.assertRaises(responder.RequestError):
            responder.parse_request(damaged, KEY, f"{HOST_NAME}:{PORT}")
        with self.assertRaises(responder.RequestError):
            responder.parse_request(signed_request() + b"x", KEY,
                                    f"{HOST_NAME}:{PORT}")

    def test_peer_and_local_addresses_must_both_be_private_ipv4(self):
        responder.validate_connection(FakeConnection(signed_request()))
        responder.validate_connection(
            FakeConnection(signed_request(), peer="192.168.1.51", local="192.168.1.11")
        )
        with self.assertRaises(responder.RequestError):
            responder.validate_connection(FakeConnection(signed_request(), peer="8.8.8.8"))
        with self.assertRaises(responder.RequestError):
            responder.validate_connection(FakeConnection(signed_request(), local="127.0.0.1"))

    def test_real_wait_listener_serves_accepted_socket_without_so_acceptconn(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            client.connect(listener.getsockname())
            client.sendall(signed_request(
                f"{HOST_NAME}:{listener.getsockname()[1]}"
            ))
            with mock.patch.object(
                responder,
                "PRIVATE_DEVICE_NETWORKS",
                (ipaddress.ip_network("127.0.0.0/8"),),
            ):
                responder.validate_listener(listener, "127.0.0.1")
                accepted, succeeded = responder.serve_listener(
                    listener,
                    self.cache,
                    self.key,
                    HOST_NAME,
                    "127.0.0.1",
                    150,
                    self.now,
                    idle_timeout_seconds=0.05,
                    max_lifetime_seconds=0.2,
                )
                self.assertEqual((accepted, succeeded), (1, 1))
                payload = parse_signed_response(client.recv(4096), NONCE)
                self.assertTrue(payload["ok"])
        finally:
            client.close()
            listener.close()

    def test_listener_is_exact_private_bind_and_bounded(self):
        connections = [FakeConnection(signed_request()) for _ in range(3)]
        listener = FakeListener(connections)
        accepted, succeeded = responder.serve_listener(
            listener,
            self.cache,
            self.key,
            HOST_NAME,
            MAC_IP,
            150,
            self.now,
            max_connections=2,
            idle_timeout_seconds=0.01,
            max_lifetime_seconds=0.1,
        )
        self.assertEqual((accepted, succeeded), (2, 2))
        self.assertEqual(listener.accept_calls, 2)
        self.assertEqual(len(listener.connections), 1)
        with self.assertRaises(responder.RequestError):
            responder.validate_listener(FakeListener([], local="192.168.1.11"), MAC_IP)
        with self.assertRaises(responder.RequestError):
            responder.validate_listener(FakeConnection(signed_request()), MAC_IP)

    def test_listener_reads_a_delayed_header_delimiter_before_authentication(self):
        request = signed_request()
        connection = FakeConnection(request)
        connection.chunks = [request[:-2], request[-2:]]
        listener = FakeListener([connection])
        accepted, succeeded = responder.serve_listener(
            listener,
            self.cache,
            self.key,
            HOST_NAME,
            MAC_IP,
            150,
            self.now,
            idle_timeout_seconds=0.01,
            max_lifetime_seconds=0.1,
        )
        self.assertEqual((accepted, succeeded), (1, 1))
        self.assertEqual(connection.recv_calls, 2)
        self.assertTrue(parse_signed_response(connection.sent[0], NONCE)["ok"])

    def test_pull_host_is_strict_local_name_or_private_ipv4(self):
        self.assertEqual(responder._pull_host(HOST_NAME), HOST_NAME)
        self.assertEqual(responder._pull_host(MAC_IP), MAC_IP)
        for value in ("DISPLAY.local", "display.example.local", "-display.local", "8.8.8.8"):
            with self.assertRaises(responder.RequestError):
                responder._pull_host(value)

    def test_inherited_accepted_socket_serves_once_then_returns(self):
        connection = FakeConnection(signed_request())
        self.assertTrue(responder.serve_once(
            connection, self.cache, self.key, HOST_NAME, 150, self.now
        ))
        self.assertEqual(connection.timeout, responder.SOCKET_TIMEOUT_SECONDS)
        self.assertEqual(connection.recv_calls, 1)
        self.assertEqual(len(connection.sent), 1)
        payload = parse_signed_response(connection.sent[0], NONCE)
        self.assertTrue(payload["ok"])

    def test_bad_peer_gets_no_quota_payload(self):
        connection = FakeConnection(signed_request(), peer="8.8.8.8")
        self.assertFalse(responder.serve_once(
            connection, self.cache, self.key, HOST_NAME, 150, self.now
        ))
        self.assertEqual(connection.sent,
                         [b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"])

    def test_timeout_and_request_bounds_fail_closed(self):
        connection = TimeoutConnection(signed_request())
        self.assertFalse(responder.serve_once(
            connection, self.cache, self.key, HOST_NAME, 150, self.now
        ))
        self.assertEqual(connection.recv_calls, 1)
        with self.assertRaises(responder.RequestError):
            responder.parse_request(
                signed_request().replace(
                    b"Connection: close\r\n",
                    b"Connection: close\r\nConnection: close\r\n",
                ),
                KEY,
                f"{HOST_NAME}:{PORT}",
            )
        with self.assertRaises(responder.RequestError):
            responder.parse_request(b"x" * (responder.MAX_REQUEST_BYTES + 1), KEY,
                                    f"{HOST_NAME}:{PORT}")

    def test_response_authentication_rejects_replay_and_tampering(self):
        body = responder.encode_payload(responder.cache_payload(self.cache, self.now, 150))
        response = responder.build_response(NONCE, body, KEY)
        self.assertTrue(parse_signed_response(response, NONCE)["ok"])
        with self.assertRaises(AssertionError):
            parse_signed_response(response, OTHER_NONCE)
        damaged = response[:-1] + bytes([response[-1] ^ 1])
        with self.assertRaises(AssertionError):
            parse_signed_response(damaged, NONCE)

    def test_self_hash_and_static_no_execution_or_egress_paths(self):
        source_path = ROOT / "imdisplay_cache_responder.py"
        digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        responder.verify_self(source_path, digest)
        with self.assertRaises(responder.ResponderError):
            responder.verify_self(source_path, "0" * 64)
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertFalse(imported.intersection({"subprocess", "urllib", "http", "requests"}))
        forbidden_attributes = {"connect", "bind", "listen", "system", "popen", "spawn"}
        forbidden_names = {"exec", "eval", "compile", "__import__"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden_attributes)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, forbidden_names)
        source = source_path.read_text(encoding="utf-8")
        self.assertNotIn("SO_ACCEPTCONN", source)
        self.assertNotIn("Codex.app", source)
        self.assertNotIn("codex-watch-token-reset", source)


class PackageLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.install_root = self.root / "cache-responder"
        self.install_root.mkdir(mode=0o700)
        self.version_root = self.install_root / manager.VERSION
        self.version_root.mkdir(mode=0o700)
        self.launch_agents = self.root / "LaunchAgents"
        self.launch_agents.mkdir(mode=0o700)
        self.key = self.install_root / "read-only-key-v1"
        write_private(self.key, f"{KEY_HEX}\n".encode("ascii"))
        self.responder_path = self.version_root / "imdisplay_cache_responder.py"
        self.plist_path = self.launch_agents / "local.imdisplay.cache-responder.plist"

    def prepare_previous(self) -> tuple[Path, bytes]:
        previous_root = self.install_root / "v2"
        previous_root.mkdir(mode=0o700)
        previous_responder = previous_root / "imdisplay_cache_responder.py"
        source = (
            (ROOT / manager.RESPONDER_NAME).read_bytes()
            + b"\n# immutable previous-version fixture\n"
        )
        previous_responder.write_bytes(source)
        previous_responder.chmod(0o500)
        digest = hashlib.sha256(source).hexdigest()
        active = plistlib.dumps({
            "Label": "legacy.imdisplay.cache-responder",
            "ProgramArguments": [
                "/usr/bin/python3",
                str(previous_responder),
                "--expected-self-sha",
                digest,
                "--cache",
                str(manager.DEFAULT_CACHE_PATH),
                "--key-file",
                str(self.key),
                "--host-name",
                HOST_NAME,
                "--inherited-fd",
                "0",
                "--max-cache-age",
                "150",
            ],
            "Sockets": {
                "QuotaHTTP": {
                    "SockFamily": "IPv4",
                    "SockType": "stream",
                    "SockProtocol": "TCP",
                    "SockServiceName": PORT,
                }
            },
            "inetdCompatibility": {"Wait": False},
            "ProcessType": "Background",
            "Umask": 63,
            "HardResourceLimits": {"Core": 0, "CPU": 5, "NumberOfFiles": 32},
        })
        write_private(self.plist_path, active)
        return previous_responder, active

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_check_only_renders_socket_only_exact_version(self):
        result = manager.check_package(ROOT)
        self.assertEqual(result["status"], "PREPARED")
        self.assertEqual(result["protocol"], 1)
        self.assertEqual(manager.VERSION, "v3")
        self.assertIn("v3", result["versionedResponder"])
        self.assertTrue(result["stagedLaunchAgent"].endswith(".v3-staged"))
        self.assertFalse(result["activated"])

    def test_install_is_versioned_socket_only_and_refuses_replacement(self):
        digest = manager.install(
            ROOT, self.responder_path, self.plist_path, manager.DEFAULT_CACHE_PATH,
            self.key, HOST_NAME, MAC_IP, PORT,
            legacy_plist=self.root / "legacy-absent"
        )
        self.assertEqual(hashlib.sha256(self.responder_path.read_bytes()).hexdigest(), digest)
        payload = plistlib.loads(self.plist_path.read_bytes())
        self.assertEqual(payload["ProgramArguments"][:4],
                         ["/usr/bin/python3", "-I", "-S", str(self.responder_path)])
        self.assertNotIn("RunAtLoad", payload)
        self.assertNotIn("KeepAlive", payload)
        self.assertNotIn("StartInterval", payload)
        self.assertEqual(payload["inetdCompatibility"], {"Wait": True})
        self.assertEqual(payload["ThrottleInterval"], 10)
        self.assertEqual(payload["Sockets"]["QuotaHTTP"]["SockNodeName"], MAC_IP)
        self.assertIn(HOST_NAME, payload["ProgramArguments"])
        with self.assertRaises(manager.PackageError):
            manager.install(
                ROOT, self.responder_path, self.plist_path, manager.DEFAULT_CACHE_PATH,
                self.key, HOST_NAME, MAC_IP, PORT,
                legacy_plist=self.root / "legacy-absent"
            )

    def test_manager_accepts_strict_local_hostname_without_reservation_flag(self):
        self.assertEqual(manager.pull_host(HOST_NAME), HOST_NAME)
        self.assertEqual(manager.pull_host(MAC_IP), MAC_IP)
        for value in ("DISPLAY.local", "display.example.local", "-display.local", "8.8.8.8"):
            with self.assertRaises(manager.PackageError):
                manager.pull_host(value)

    def test_stage_upgrade_preserves_previous_and_prepares_exact_v3(self):
        previous_responder, active = self.prepare_previous()
        staged_plist = self.launch_agents / f"{self.plist_path.name}.v3-staged"
        disabled_previous = self.launch_agents / f"{self.plist_path.name}.v2-disabled"
        digest = manager.stage_upgrade(
            ROOT,
            self.responder_path,
            staged_plist,
            self.plist_path,
            previous_responder,
            disabled_previous,
            manager.DEFAULT_CACHE_PATH,
            self.key,
            HOST_NAME,
            MAC_IP,
            PORT,
            legacy_plist=self.root / "legacy-absent",
        )
        self.assertEqual(self.plist_path.read_bytes(), active)
        self.assertNotEqual(previous_responder.read_bytes(), self.responder_path.read_bytes())
        self.assertEqual(previous_responder.stat().st_mode & 0o777, 0o500)
        self.assertEqual(staged_plist.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.responder_path.stat().st_mode & 0o777, 0o500)
        self.assertEqual(hashlib.sha256(self.responder_path.read_bytes()).hexdigest(), digest)
        payload = plistlib.loads(staged_plist.read_bytes())
        self.assertEqual(payload["ProgramArguments"][:4],
                         ["/usr/bin/python3", "-I", "-S", str(self.responder_path)])
        self.assertNotIn("RunAtLoad", payload)
        self.assertNotIn("KeepAlive", payload)
        self.assertNotIn("StartInterval", payload)
        with self.assertRaises(manager.PackageError):
            manager.stage_upgrade(
                ROOT,
                self.responder_path,
                staged_plist,
                self.plist_path,
                previous_responder,
                disabled_previous,
                manager.DEFAULT_CACHE_PATH,
                self.key,
                HOST_NAME,
                MAC_IP,
                PORT,
                legacy_plist=self.root / "legacy-absent",
            )

    def test_stage_upgrade_rejects_unexpected_previous_plist(self):
        previous_responder, _ = self.prepare_previous()
        self.plist_path.write_bytes(b"unexpected")
        with self.assertRaisesRegex(manager.PackageError, "active previous plist"):
            manager.stage_upgrade(
                ROOT,
                self.responder_path,
                self.launch_agents / f"{self.plist_path.name}.v3-staged",
                self.plist_path,
                previous_responder,
                self.launch_agents / f"{self.plist_path.name}.v2-disabled",
                manager.DEFAULT_CACHE_PATH,
                self.key,
                HOST_NAME,
                MAC_IP,
                PORT,
                legacy_plist=self.root / "legacy-absent",
            )

    def test_atomic_write_rejects_unsafe_parent_without_chmod(self):
        unsafe = self.root / "unsafe"
        unsafe.mkdir(mode=0o755)
        original_mode = unsafe.stat().st_mode & 0o777
        with self.assertRaises(manager.PackageError):
            manager._atomic_write(unsafe / "value", b"x", 0o600, replace=False)
        self.assertEqual(unsafe.stat().st_mode & 0o777, original_mode)
        self.assertFalse((unsafe / "value").exists())

    def test_prepare_and_rollback_preserve_recoverability(self):
        prepared_key = self.install_root / "prepared-key"
        provisioning = self.root / "provisioning.json"
        manager.prepare_key(prepared_key, provisioning, HOST_NAME, PORT)
        self.assertEqual(prepared_key.stat().st_mode & 0o777, 0o600)
        config = json.loads(provisioning.read_text(encoding="ascii"))
        self.assertEqual(config["kind"], "budget_pull_config")
        self.assertEqual(config["macHost"], HOST_NAME)
        self.assertNotIn("macAddress", config)
        self.assertFalse(config["legacyPushEnabled"])
        self.plist_path.write_text("staged", encoding="ascii")
        self.plist_path.chmod(0o600)
        disabled = manager.rollback(self.plist_path)
        self.assertTrue(disabled.exists())
        self.assertFalse(self.plist_path.exists())


if __name__ == "__main__":
    unittest.main()
