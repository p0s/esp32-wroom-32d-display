#!/usr/bin/python3
from __future__ import annotations

import ast
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "codex_quota_cache_producer.py"
SPEC = importlib.util.spec_from_file_location("imdisplay_budget_cache", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def timestamp(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def token_event(
    epoch: int,
    used: float = 25.0,
    *,
    ordinal: int = 1,
    secondary: bool = True,
    unknown_rate_key: bool = False,
) -> dict[str, object]:
    rate_limits: dict[str, object] = {
        "limit_id": "codex",
        "primary": {
            "used_percent": used,
            "window_minutes": 300,
            "resets_at": epoch + 1800,
        },
        "credits": {"balance": "private-value-never-persisted"},
        "plan_type": "private-plan-never-persisted",
    }
    if secondary:
        rate_limits["secondary"] = {
            "used_percent": min(100.0, used + 10.0),
            "window_minutes": 10_080,
            "resets_at": epoch + 86_400,
        }
    if unknown_rate_key:
        rate_limits["unexpected"] = "reject"
    return {
        "timestamp": timestamp(epoch),
        "type": "event_msg",
        "ordinal": ordinal,
        "payload": {
            "type": "token_count",
            "info": {"privatePrompt": "must-never-persist"},
            "rate_limits": rate_limits,
        },
    }


def current_schema_token_event(epoch: int, used: float = 25.0) -> dict[str, object]:
    event = token_event(epoch, used=used, secondary=False)
    rate_limits = event["payload"]["rate_limits"]
    assert isinstance(rate_limits, dict)
    rate_limits.update(
        {
            "individual_limit": None,
            "limit_name": None,
            "rate_limit_reached_type": None,
            "secondary": None,
            "spend_control_reached": None,
        }
    )
    return event


def encode_line(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"


class ProducerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.transcript_root = self.base / "sessions"
        self.transcript_root.mkdir(mode=0o700)
        self.transcript = self.transcript_root / "rollout-test.jsonl"
        self.cache_path = self.base / "cache" / "codex-budget.json"
        self.now = int(time.time())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_transcript(self, *values: bytes) -> None:
        self.transcript.write_bytes(b"".join(values))
        self.transcript.chmod(0o600)

    def produce(self, kind: str = "stop", now: int | None = None) -> bool:
        return MODULE.produce_cache(
            str(self.transcript),
            kind,
            cache_path=self.cache_path,
            transcript_root=self.transcript_root,
            now=self.now if now is None else now,
        )

    def read_cache(self) -> dict[str, object]:
        return json.loads(self.cache_path.read_text())

    def test_valid_schema_permissions_privacy_and_size(self) -> None:
        self.write_transcript(encode_line(token_event(self.now - 2, used=21.25)))
        self.assertTrue(self.produce())
        raw = self.cache_path.read_bytes()
        parsed = self.read_cache()
        self.assertEqual({"schemaVersion", "checkedAt", "windows"}, set(parsed))
        self.assertEqual(2, len(parsed["windows"]))
        self.assertEqual(78.8, parsed["windows"][0]["remainingPercent"])
        self.assertEqual("codex", parsed["windows"][0]["limitId"])
        self.assertNotIn(b"private", raw)
        self.assertLessEqual(len(raw), MODULE.MAX_CACHE_BYTES)
        directory_mode = stat.S_IMODE(os.lstat(self.cache_path.parent).st_mode)
        file_mode = stat.S_IMODE(os.lstat(self.cache_path).st_mode)
        self.assertEqual(0o700, directory_mode)
        self.assertEqual(0o600, file_mode)
        self.assertTrue(stat.S_ISREG(os.lstat(self.cache_path).st_mode))

    def test_current_transcript_schema_with_nullable_extensions(self) -> None:
        event = current_schema_token_event(self.now - 2, used=37.5)
        rate_limits = event["payload"]["rate_limits"]
        self.assertEqual(
            {
                "credits",
                "individual_limit",
                "limit_id",
                "limit_name",
                "plan_type",
                "primary",
                "rate_limit_reached_type",
                "secondary",
                "spend_control_reached",
            },
            set(rate_limits),
        )
        self.write_transcript(encode_line(event))
        self.assertTrue(self.produce())
        cache = self.read_cache()
        self.assertEqual(1, len(cache["windows"]))
        self.assertEqual("Codex", cache["windows"][0]["limitName"])
        self.assertEqual(62.5, cache["windows"][0]["remainingPercent"])

    def test_newest_valid_event_wins_and_unknown_schema_is_skipped(self) -> None:
        old = token_event(self.now - 30, used=10.0, ordinal=1)
        invalid_new = token_event(
            self.now - 10, used=99.0, ordinal=2, unknown_rate_key=True
        )
        newest = token_event(self.now - 5, used=40.0, ordinal=3)
        self.write_transcript(encode_line(old), encode_line(invalid_new), encode_line(newest))
        self.assertTrue(self.produce())
        self.assertEqual(60.0, self.read_cache()["windows"][0]["remainingPercent"])

    def test_malformed_and_truncated_tail_preserve_previous_valid_event(self) -> None:
        valid = encode_line(token_event(self.now - 4, used=30.0))
        self.write_transcript(valid, b'{"type":"event_msg","payload":')
        self.assertTrue(self.produce())
        self.assertEqual(70.0, self.read_cache()["windows"][0]["remainingPercent"])

    def test_bounded_tail_does_not_scan_unbounded_history(self) -> None:
        self.write_transcript(
            encode_line(token_event(self.now - 5)),
            b"x" * 4096,
            b"\n",
        )
        with self.assertRaises(MODULE.ProducerError):
            MODULE.produce_cache(
                str(self.transcript),
                "stop",
                cache_path=self.cache_path,
                transcript_root=self.transcript_root,
                tail_bytes=1024,
            )
        self.assertFalse(self.cache_path.exists())

    def test_transcript_symlink_and_directory_are_rejected(self) -> None:
        outside = self.base / "outside.jsonl"
        outside.write_bytes(encode_line(token_event(self.now - 2)))
        outside.chmod(0o600)
        symlink = self.transcript_root / "linked.jsonl"
        symlink.symlink_to(outside)
        with self.assertRaises(MODULE.ProducerError):
            MODULE.produce_cache(
                str(symlink),
                "stop",
                cache_path=self.cache_path,
                transcript_root=self.transcript_root,
            )
        directory = self.transcript_root / "directory.jsonl"
        directory.mkdir()
        with self.assertRaises(MODULE.ProducerError):
            MODULE.produce_cache(
                str(directory),
                "stop",
                cache_path=self.cache_path,
                transcript_root=self.transcript_root,
            )
        unsafe_directory = self.transcript_root / "unsafe"
        unsafe_directory.mkdir(mode=0o700)
        unsafe_directory.chmod(0o777)
        unsafe_transcript = unsafe_directory / "rollout.jsonl"
        unsafe_transcript.write_bytes(encode_line(token_event(self.now - 2)))
        unsafe_transcript.chmod(0o600)
        with self.assertRaises(MODULE.ProducerError):
            MODULE.produce_cache(
                str(unsafe_transcript),
                "stop",
                cache_path=self.cache_path,
                transcript_root=self.transcript_root,
            )

    def test_symlinked_cache_directory_is_rejected(self) -> None:
        self.write_transcript(encode_line(token_event(self.now - 2)))
        referent = self.base / "cache-referent"
        referent.mkdir(mode=0o700)
        self.cache_path.parent.symlink_to(referent)
        with self.assertRaises(MODULE.ProducerError):
            self.produce()
        self.assertEqual([], list(referent.iterdir()))

    def test_malformed_oversize_and_symlink_caches_are_preserved(self) -> None:
        self.write_transcript(encode_line(token_event(self.now - 2)))
        self.cache_path.parent.mkdir(mode=0o700)
        for original in (b"not-json", b"x" * (MODULE.MAX_CACHE_BYTES + 1)):
            if self.cache_path.exists() or self.cache_path.is_symlink():
                self.cache_path.unlink()
            self.cache_path.write_bytes(original)
            self.cache_path.chmod(0o600)
            with self.assertRaises(MODULE.ProducerError):
                self.produce()
            self.assertEqual(original, self.cache_path.read_bytes())
        self.cache_path.unlink()
        referent = self.base / "referent"
        referent.write_bytes(b"untouched")
        self.cache_path.symlink_to(referent)
        with self.assertRaises(MODULE.ProducerError):
            self.produce()
        self.assertEqual(b"untouched", referent.read_bytes())

    def test_wrong_cache_permissions_fail_closed(self) -> None:
        self.write_transcript(encode_line(token_event(self.now - 2)))
        self.cache_path.parent.mkdir(mode=0o700)
        self.cache_path.write_bytes(encode_line({"invalid": True}))
        self.cache_path.chmod(0o644)
        before = self.cache_path.read_bytes()
        with self.assertRaises(MODULE.ProducerError):
            self.produce()
        self.assertEqual(before, self.cache_path.read_bytes())

    def test_older_event_cannot_regress_newer_cache(self) -> None:
        self.write_transcript(encode_line(token_event(self.now - 2, used=50.0)))
        self.assertTrue(self.produce())
        before = self.cache_path.read_bytes()
        self.write_transcript(encode_line(token_event(self.now - 20, used=1.0)))
        self.assertFalse(self.produce())
        self.assertEqual(before, self.cache_path.read_bytes())

    def test_future_event_fails_without_creating_cache(self) -> None:
        self.write_transcript(
            encode_line(
                token_event(
                    self.now + MODULE.MAX_FUTURE_SKEW_SECONDS + 30,
                    used=50.0,
                )
            )
        )
        with self.assertRaises(MODULE.ProducerError):
            self.produce()
        self.assertFalse(self.cache_path.exists())

    def test_post_tool_coalesces_but_stop_makes_final_attempt(self) -> None:
        self.write_transcript(encode_line(token_event(self.now - 3, used=20.0)))
        self.assertTrue(self.produce("post-tool-use", self.now))
        before = self.cache_path.read_bytes()
        self.write_transcript(encode_line(token_event(self.now - 1, used=45.0)))
        self.assertFalse(self.produce("post-tool-use", self.now + 10))
        self.assertEqual(before, self.cache_path.read_bytes())
        self.assertTrue(self.produce("stop", self.now + 10))
        self.assertEqual(55.0, self.read_cache()["windows"][0]["remainingPercent"])

    def test_owner_only_lock_rejects_overlap(self) -> None:
        self.write_transcript(encode_line(token_event(self.now - 2)))
        directory_descriptor = MODULE._validate_cache_directory(self.cache_path)
        lock_descriptor = MODULE._open_lock(directory_descriptor)
        try:
            with self.assertRaises(MODULE.LockBusy):
                self.produce()
            lock_mode = stat.S_IMODE(os.fstat(lock_descriptor).st_mode)
            self.assertEqual(0o600, lock_mode)
        finally:
            os.close(lock_descriptor)
            os.close(directory_descriptor)

    def test_short_cache_write_fails_closed_and_leaves_no_partial_file(self) -> None:
        directory_descriptor = MODULE._validate_cache_directory(self.cache_path)
        try:
            with mock.patch.object(MODULE.os, "write", return_value=0):
                with self.assertRaisesRegex(MODULE.ProducerError, "short cache write"):
                    MODULE._atomic_write_cache(directory_descriptor, b"{}\n")
        finally:
            os.close(directory_descriptor)
        self.assertFalse(self.cache_path.exists())
        self.assertEqual([], list(self.cache_path.parent.iterdir()))

    def test_hook_stdout_exit_and_self_hash_behavior(self) -> None:
        self.write_transcript(encode_line(token_event(self.now - 2)))
        digest = hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
        post_input = io.BytesIO(
            encode_line(
                {
                    "hook_event_name": "PostToolUse",
                    "transcript_path": str(self.transcript),
                }
            )
        )
        post_output = io.StringIO()
        code = MODULE.main(
            ["--expected-self-sha", digest, "hook", "--event", "post-tool-use"],
            stdin=post_input,
            stdout=post_output,
            script_path=SCRIPT,
            cache_path=self.cache_path,
            transcript_root=self.transcript_root,
        )
        self.assertEqual(0, code)
        self.assertEqual("", post_output.getvalue())
        stop_output = io.StringIO()
        code = MODULE.main(
            ["--expected-self-sha", "0" * 64, "hook", "--event", "stop"],
            stdin=io.BytesIO(b"malformed"),
            stdout=stop_output,
            script_path=SCRIPT,
            cache_path=self.cache_path,
            transcript_root=self.transcript_root,
        )
        self.assertEqual(0, code)
        self.assertEqual("{}\n", stop_output.getvalue())

    def test_no_network_subprocess_or_executable_path_primitives(self) -> None:
        source = SCRIPT.read_text()
        tree = ast.parse(source)
        forbidden_imports = {"http", "requests", "socket", "subprocess", "urllib"}
        imported: set[str] = set()
        forbidden_calls: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "os":
                    if node.func.attr == "system" or node.func.attr.startswith(("exec", "spawn")):
                        forbidden_calls.add(node.func.attr)
        self.assertFalse(imported & forbidden_imports)
        self.assertFalse(forbidden_calls)
        self.assertNotIn("/bin/", source)


if __name__ == "__main__":
    unittest.main()
