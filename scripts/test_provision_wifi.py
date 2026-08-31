import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import provision_wifi


class ProvisionWifiTests(unittest.TestCase):
    def test_payload_is_one_versioned_json_line(self):
        payload = provision_wifi.make_payload("Local Wi-Fi", "private-pass")
        self.assertEqual(payload.count(b"\n"), 1)
        parsed = json.loads(payload)
        self.assertEqual(parsed["kind"], "wifi_config")
        self.assertEqual(parsed["ssid"], "Local Wi-Fi")

    def test_password_length_is_validated(self):
        with self.assertRaisesRegex(ValueError, "8 to 63"):
            provision_wifi.make_payload("Local Wi-Fi", "short")

    def test_loads_private_env_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("WIFI_NAME='Local Wi-Fi'\nWIFI_PASSWORD=private-pass\n")
            path.chmod(0o600)
            self.assertEqual(
                provision_wifi.load_env_file(path),
                {"WIFI_NAME": "Local Wi-Fi", "WIFI_PASSWORD": "private-pass"},
            )

    def test_rejects_readable_env_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("WIFI_PASSWORD=private-pass\n")
            path.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "owner-only"):
                provision_wifi.load_env_file(path)

    def test_connection_parser_ignores_private_boot_line(self):
        uart = mock.MagicMock()
        uart.readline.side_effect = [
            b"control AP=private password=private ip=192.168.4.1 ready=1\n",
            b"local wifi connected=1 ip=192.168.1.42\n",
        ]
        self.assertEqual(provision_wifi.wait_for_address(uart, 1), "192.168.1.42")


if __name__ == "__main__":
    unittest.main()
