import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import install_custom_firmware as installer


class InstallGateTests(unittest.TestCase):
    def make_gate(self, root: Path) -> Path:
        first = root / "factory-1.bin"
        second = root / "factory-2.bin"
        with first.open("wb") as handle:
            handle.truncate(16 * 1024 * 1024)
        with second.open("wb") as handle:
            handle.truncate(16 * 1024 * 1024)
        artifacts = []
        for address, name in ((0x1000, "boot.bin"), (0x8000, "parts.bin"),
                              (0xD000, "ota.bin"), (0x10000, "app.bin")):
            artifacts.append({"address": hex(address), "path": str(root / name), "sha256": "artifact"})
        gate = {
            "schemaVersion": 1,
            "boardMac": "02:00:00:00:00:01",
            "security": {"secureBoot": False, "flashEncryption": False, "uartDownloadEnabled": True},
            "backup": {"first": str(first), "second": str(second), "sha256": "backup"},
            "artifacts": artifacts,
        }
        path = root / "gate.json"
        path.write_text(json.dumps(gate), encoding="utf-8")
        return path

    @mock.patch.object(installer, "require_file_hash")
    def test_accepts_only_exact_board_security_and_address_map(self, require_hash):
        with tempfile.TemporaryDirectory() as directory:
            gate, artifacts = installer.load_gate(
                self.make_gate(Path(directory)), "02:00:00:00:00:01"
            )
        self.assertEqual(gate["boardMac"], "02:00:00:00:00:01")
        self.assertEqual([address for address, _ in artifacts], [0x1000, 0x8000, 0xD000, 0x10000])
        self.assertEqual(require_hash.call_count, 6)

    def test_rejects_unsafe_security_state_before_any_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_gate(Path(directory))
            gate = json.loads(path.read_text())
            gate["security"]["secureBoot"] = True
            path.write_text(json.dumps(gate))
            with self.assertRaisesRegex(RuntimeError, "security gate"):
                installer.load_gate(path, "02:00:00:00:00:01")

    def test_rejects_a_different_or_noncanonical_board_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_gate(Path(directory))
            with self.assertRaisesRegex(RuntimeError, "not for this ESP32"):
                installer.load_gate(path, "02:00:00:00:00:02")
            with self.assertRaisesRegex(RuntimeError, "canonical"):
                installer.load_gate(path, "02-00-00-00-00-01")


if __name__ == "__main__":
    unittest.main()
