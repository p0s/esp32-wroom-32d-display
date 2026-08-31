import base64
import hashlib
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import wifi_ota


class WifiOtaTests(unittest.TestCase):
    def test_loads_only_exact_ready_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential.json"
            path.write_text(
                json.dumps(
                    {
                        "accessPoint": "XSURE-CODEX-CE8C",
                        "password": "XS23456789ABCD",
                        "address": "192.168.4.1",
                        "ready": True,
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(wifi_ota.load_credential(path)["address"], "192.168.4.1")

    def test_runtime_credential_remains_valid_while_recovery_ap_is_off(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential.json"
            path.write_text(
                json.dumps(
                    {
                        "accessPoint": "imDisplay-1234",
                        "password": "device-password",
                        "address": "192.168.1.50",
                        "ready": True,
                        "accessPointReady": False,
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                wifi_ota.load_credential(path)["address"], "192.168.1.50"
            )

    def test_request_is_authenticated_no_store_multipart(self):
        with tempfile.TemporaryDirectory() as directory:
            firmware = Path(directory) / "firmware.bin"
            firmware.write_bytes(b"firmware-bytes")
            request = wifi_ota.make_request("http://192.168.4.1/update", "secret", firmware)
        parsed = urllib.parse.urlsplit(request.full_url)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(
            urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")),
            "http://192.168.4.1/update",
        )
        self.assertEqual(query["size"], [str(len(b"firmware-bytes"))])
        self.assertEqual(query["sha256"], [hashlib.sha256(b"firmware-bytes").hexdigest()])
        self.assertEqual(
            request.get_header("Authorization"),
            "Basic " + base64.b64encode(b"xsure:secret").decode("ascii"),
        )
        self.assertEqual(request.get_header("Cache-control"), "no-store")
        self.assertIn("multipart/form-data", request.get_header("Content-type"))
        self.assertIn(b'name="firmware"', request.data)
        self.assertIn(b"firmware-bytes", request.data)

    def test_rejects_firmware_larger_than_ota_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            firmware = Path(directory) / "firmware.bin"
            with firmware.open("wb") as handle:
                handle.truncate(wifi_ota.MAX_FIRMWARE_BYTES + 1)
            with self.assertRaisesRegex(RuntimeError, "outside the preserved OTA slot"):
                wifi_ota.firmware_metadata(firmware)

    @mock.patch.object(wifi_ota.urllib.request, "urlopen")
    def test_upload_returns_device_digest_verification_readback(self, urlopen):
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = (
            b"Update installed and SHA-256 verified. Rebooting.\n"
        )
        urlopen.return_value.__enter__.return_value = response
        request = mock.MagicMock()
        self.assertIn("SHA-256 verified", wifi_ota.upload_firmware(request))
        urlopen.assert_called_once_with(request, timeout=120)

    @mock.patch.object(wifi_ota.urllib.request, "urlopen")
    def test_upload_rejects_missing_digest_readback_by_default(self, urlopen):
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = b"Update installed. Rebooting.\n"
        urlopen.return_value.__enter__.return_value = response
        with self.assertRaisesRegex(RuntimeError, "lacked SHA-256"):
            wifi_ota.upload_firmware(mock.MagicMock())
        self.assertIn(
            "Update installed",
            wifi_ota.upload_firmware(
                mock.MagicMock(), require_digest_readback=False
            ),
        )

    def test_loads_private_lan_address(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lan.json"
            path.write_text(json.dumps({"address": "192.168.1.50"}))
            self.assertEqual(wifi_ota.load_private_address(path), "192.168.1.50")

    @mock.patch.object(wifi_ota.urllib.request, "urlopen")
    @mock.patch.object(wifi_ota.time, "sleep")
    def test_wait_for_version_uses_authenticated_lan_readback(self, _sleep, urlopen):
        old = mock.MagicMock()
        old.status = 200
        old.read.return_value = b'{"firmware":"1.3.4"}'
        current = mock.MagicMock()
        current.status = 200
        current.read.return_value = b'{"firmware":"1.4.0"}'
        urlopen.return_value.__enter__.side_effect = [old, current]
        wifi_ota.wait_for_version("192.168.1.50", "secret", "1.4.0", 5)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://192.168.1.50/api/state")
        self.assertEqual(request.get_header("Authorization"), "Basic eHN1cmU6c2VjcmV0")

    @mock.patch.object(wifi_ota.socket, "create_connection")
    @mock.patch.object(wifi_ota.time, "sleep")
    def test_wait_retries_locally_until_device_is_reachable(self, _sleep, connect):
        connection = mock.MagicMock()
        connect.side_effect = [OSError("offline"), connection]
        wifi_ota.wait_for_device("192.168.4.1", 10)
        self.assertEqual(connect.call_count, 2)
        connection.__enter__.assert_called_once()

    @mock.patch.object(wifi_ota.urllib.request, "urlopen")
    def test_post_boot_self_test_requires_navigation_transitions_and_counters(self, urlopen):
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = json.dumps(
            {
                "pass": True,
                "schema": 4,
                "releaseDelta": 8,
                "shortPressDelta": 4,
                "doubleClickDelta": 2,
                "longPressDelta": 18,
                "secondPressLatchDelta": 2,
                "navigationPassed": True,
                "pageToLauncher": True,
                "launcherToPage": True,
                "nestedBack": True,
                "longPressBack": True,
                "doubleClickFallback": True,
                "secondPressGrace": True,
                "queuedPulse": True,
                "edgeQueueHealthy": True,
                "stateRestored": True,
                "pageRoundTrips": 9,
                "pageRoundTripsExpected": 9,
                "backActions": 4,
                "backActionsExpected": 4,
                "failure": "NONE",
                "failurePage": "NONE",
            }
        ).encode()
        urlopen.return_value.__enter__.return_value = response
        wifi_ota.run_device_self_test("192.168.1.50", "secret")
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url, "http://192.168.1.50/api/self-test?input=1"
        )
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Basic eHN1cmU6c2VjcmV0")

    @mock.patch.object(wifi_ota.urllib.request, "urlopen")
    def test_post_boot_self_test_rejects_navigation_failure(self, urlopen):
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = b'{"pass":true,"schema":3,"navigationPassed":false}'
        urlopen.return_value.__enter__.return_value = response
        with self.assertRaisesRegex(RuntimeError, "input/navigation"):
            wifi_ota.run_device_self_test("192.168.1.50", "secret")


if __name__ == "__main__":
    unittest.main()
