import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import lan_control


class LanControlTests(unittest.TestCase):
    def test_authenticated_request_uses_post_and_no_store(self):
        request = lan_control.authenticated_request(
            "http://192.168.1.50/api/control?page=about", "secret", "POST"
        )
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.data, b"")
        self.assertEqual(request.get_header("Cache-control"), "no-store")
        self.assertEqual(request.get_header("Authorization"), "Basic eHN1cmU6c2VjcmV0")

    @mock.patch.object(lan_control.time, "sleep")
    @mock.patch.object(lan_control, "read_state")
    @mock.patch.object(lan_control, "open_request", return_value=(204, b""))
    def test_select_page_verifies_readback(self, _open, read_state, _sleep):
        read_state.return_value = {"page": "ABOUT", "menu": False}
        self.assertEqual(
            lan_control.select_page("http://192.168.1.50", "secret", "about")["page"],
            "ABOUT",
        )

    @mock.patch.object(lan_control.time, "sleep")
    @mock.patch.object(lan_control, "read_state", return_value={"page": "ABOUT", "menu": True})
    @mock.patch.object(lan_control, "open_request", return_value=(204, b""))
    def test_select_menu_verifies_readback(self, _open, _state, _sleep):
        self.assertTrue(
            lan_control.select_page("http://192.168.1.50", "secret", "menu")["menu"]
        )

    @mock.patch.object(lan_control.time, "sleep")
    @mock.patch.object(lan_control, "post_query")
    @mock.patch.object(lan_control, "read_state")
    def test_virtual_knob_action_requires_matching_readback(
        self, read_state, post_query, _sleep
    ):
        read_state.side_effect = [
            {"input": {"remoteEvents": 4}},
            {"input": {"remoteEvents": 5, "lastRemoteAction": "PRESS"}},
        ]
        state = lan_control.send_input(
            "http://192.168.1.50", "secret", "press"
        )
        self.assertEqual(state["input"]["remoteEvents"], 5)
        post_query.assert_called_once_with(
            "http://192.168.1.50", "secret", "/api/input", {"action": "press"}
        )

    def test_virtual_knob_rejects_unknown_action(self):
        with self.assertRaisesRegex(ValueError, "unknown virtual knob action"):
            lan_control.send_input("http://192.168.1.50", "secret", "explode")

    @mock.patch.object(lan_control, "read_state")
    @mock.patch.object(lan_control, "open_request")
    def test_input_self_test_requires_navigation_transitions_and_counters(
        self, open_request, read_state
    ):
        result = {
            "pass": True,
            "schema": 3,
            "releaseDelta": 40,
            "shortPressDelta": 4,
            "doubleClickDelta": 18,
            "longPressDelta": 1,
            "secondPressLatchDelta": 18,
            "navigationPassed": True,
            "pageToLauncher": True,
            "launcherToPage": True,
            "nestedBack": True,
            "longPressMenu": True,
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
        open_request.return_value = (200, json.dumps(result).encode())
        read_state.return_value = {
            "selfTest": {
                "schema": 3,
                "inputPassed": True,
                "navigationPassed": True,
                "pageRoundTrips": 9,
                "pageRoundTripsExpected": 9,
                "backActions": 4,
                "backActionsExpected": 4,
                "queuedPulse": True,
                "edgeQueueHealthy": True,
                "stateRestored": True,
                "failure": "NONE",
                "failurePage": "NONE",
            }
        }
        state = lan_control.run_input_self_test(
            "http://192.168.1.50", "secret"
        )
        self.assertTrue(state["selfTest"]["inputPassed"])
        request = open_request.call_args.args[0]
        self.assertEqual(
            request.full_url, "http://192.168.1.50/api/self-test?input=1"
        )
        self.assertEqual(request.method, "POST")

    @mock.patch.object(lan_control, "read_state")
    @mock.patch.object(lan_control, "open_request")
    def test_input_self_test_rejects_counter_only_legacy_result(
        self, open_request, _read_state
    ):
        open_request.return_value = (
            200,
            b'{"pass":true,"releaseDelta":2,"doubleClickDelta":1,"longPressDelta":1}',
        )
        with self.assertRaisesRegex(RuntimeError, "evidence did not match"):
            lan_control.run_input_self_test("http://192.168.1.50", "secret")

    def test_reads_applet_install_state(self):
        state = {"applets": [{"id": "codex", "installed": True}]}
        self.assertTrue(lan_control.applet_installed(state, "codex"))
        with self.assertRaisesRegex(RuntimeError, "missing applet"):
            lan_control.applet_installed(state, "timer")

    def test_extracts_only_persistent_product_settings(self):
        state = {
            "applets": [
                {"id": "codex", "installed": True},
                {"id": "timer", "installed": False},
            ],
            "leds": {
                "preset": "WARM",
                "brightness": 20,
                "feedback": True,
                "ready": True,
            },
            "display": {"brightness": 100, "backlightDuty": 255},
            "sound": {"profile": "MINIMAL", "volume": 20, "driverReady": True},
            "timer": {"durationMinutes": 25, "remainingSeconds": 300},
            "accessPointEnabled": False,
            "bootId": 4,
        }
        self.assertEqual(
            lan_control.persistent_settings(state),
            {
                "applets": {"codex": True, "timer": False},
                "leds": {"preset": "WARM", "brightness": 20, "feedback": True},
                "displayBrightness": 100,
                "sound": {"profile": "MINIMAL", "volume": 20},
                "timerMinutes": 25,
                "recoveryWifi": False,
            },
        )

    @mock.patch.object(lan_control.time, "sleep")
    @mock.patch.object(lan_control, "read_state")
    @mock.patch.object(lan_control, "post_query")
    def test_sets_recovery_wifi_with_effective_readback(
        self, post_query, read_state, _sleep
    ):
        read_state.side_effect = [
            {"accessPointEnabled": True, "accessPointReady": False},
            {"accessPointEnabled": True, "accessPointReady": True},
        ]
        state = lan_control.set_recovery_wifi(
            "http://192.168.1.50", "secret", True
        )
        self.assertTrue(state["accessPointReady"])
        post_query.assert_called_once_with(
            "http://192.168.1.50",
            "secret",
            "/api/recovery-wifi",
            {"enabled": 1},
            expected_status=202,
        )

    @mock.patch.object(lan_control, "read_state")
    @mock.patch.object(lan_control, "post_query")
    def test_sets_led_preset_and_brightness_with_readback(self, post_query, read_state):
        read_state.return_value = {"leds": {"preset": "WARM", "brightness": 20}}
        state = lan_control.set_leds(
            "http://192.168.1.50", "secret", "warm", 20
        )
        post_query.assert_called_once_with(
            "http://192.168.1.50",
            "secret",
            "/api/leds",
            {"preset": "warm", "brightness": 20},
        )
        self.assertEqual(state["leds"]["preset"], "WARM")

    def test_safe_state_omits_network_identity(self):
        state = {
            "product": "imDisplay",
            "firmware": "2.0.1",
            "stationSsid": "private-network",
            "stationIp": "192.168.1.50",
            "accessPointEnabled": False,
            "accessPointReady": False,
            "remaining": 75,
            "freeHeap": 123456,
            "minFreeHeap": 98765,
            "persistence": {"sessions": 2, "keyWriteAttempts": 3, "failures": 0},
        }
        self.assertEqual(
            lan_control.safe_state(state),
            {
                "product": "imDisplay",
                "firmware": "2.0.1",
                "remaining": 75,
                "freeHeap": 123456,
                "minFreeHeap": 98765,
                "accessPointEnabled": False,
                "accessPointReady": False,
                "persistence": {"sessions": 2, "keyWriteAttempts": 3, "failures": 0},
            },
        )

    def test_safe_state_reports_pull_status_without_endpoint_or_key(self):
        state = {
            "budgetPull": {
                "protocol": 1,
                "configured": True,
                "enabled": True,
                "legacyPushEnabled": False,
                "lastResult": "fresh",
                "discovery": "runtime-mdns-or-private-ip",
                "macHost": "imdisplay-mac.local",
                "macPort": 47832,
                "readOnlyKey": "forbidden",
            }
        }
        self.assertEqual(
            lan_control.safe_state(state),
            {
                "budgetPull": {
                    "protocol": 1,
                    "configured": True,
                    "enabled": True,
                    "legacyPushEnabled": False,
                    "lastResult": "fresh",
                    "discovery": "runtime-mdns-or-private-ip",
                }
            },
        )

    @mock.patch.object(lan_control, "read_state")
    @mock.patch.object(lan_control, "open_request", return_value=(204, b""))
    def test_provisions_owner_only_hostname_config_without_safe_disclosure(
        self, open_request, read_state
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            path = root / "pull.json"
            config = {
                "schema": 1,
                "kind": "budget_pull_config",
                "enabled": True,
                "macHost": "imdisplay-mac.local",
                "macPort": 47832,
                "readOnlyKey": "a" * 64,
                "legacyPushEnabled": False,
            }
            path.write_text(json.dumps(config), encoding="ascii")
            path.chmod(0o600)
            read_state.return_value = {
                "budgetPull": {
                    "configured": True,
                    "enabled": True,
                    "legacyPushEnabled": False,
                    "macHost": "imdisplay-mac.local",
                    "macPort": 47832,
                }
            }
            state = lan_control.provision_budget_source(
                "http://192.168.1.50", "secret", path
            )
            request = open_request.call_args.args[0]
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.full_url, "http://192.168.1.50/api/budget-source")
            self.assertNotIn("macHost", lan_control.safe_state(state)["budgetPull"])
            self.assertNotIn("macPort", lan_control.safe_state(state)["budgetPull"])

    def test_rejects_unsafe_or_nonlocal_provisioning_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            path = root / "pull.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "kind": "budget_pull_config",
                        "enabled": True,
                        "macHost": "IMDISPLAY-MAC.local",
                        "macPort": 47832,
                        "readOnlyKey": "a" * 64,
                        "legacyPushEnabled": False,
                    }
                ),
                encoding="ascii",
            )
            path.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "invalid budget source"):
                lan_control.load_budget_source_config(path)
            path.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "not owner-only"):
                lan_control.load_budget_source_config(path)

    def test_budget_source_host_accepts_private_ipv4_compatibility(self):
        self.assertTrue(lan_control.valid_pull_host("imdisplay-mac.local"))
        self.assertTrue(lan_control.valid_pull_host("192.168.1.10"))
        self.assertFalse(lan_control.valid_pull_host("8.8.8.8"))
        self.assertFalse(lan_control.valid_pull_host("IMDISPLAY-MAC.local"))

    def test_verifies_authenticated_device_address(self):
        state = {
            "stationConnected": True,
            "stationIp": "192.168.1.50",
        }
        self.assertIs(
            lan_control.verify_device_address(state, "192.168.1.50"), state
        )
        with self.assertRaisesRegex(RuntimeError, "address readback did not match"):
            lan_control.verify_device_address(state, "192.168.1.51")

    def test_syncs_public_metadata_without_changing_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential.json"
            path.write_text(
                json.dumps(
                    {
                        "accessPoint": "XSURE-OLD",
                        "password": "private-password",
                        "address": "192.168.4.1",
                        "ready": True,
                    }
                ),
                encoding="utf-8",
            )
            lan_control.sync_credential_metadata(
                path,
                "192.168.1.50",
                {"accessPoint": "imDisplay-1234", "accessPointReady": True},
            )
            updated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(updated["password"], "private-password")
            self.assertEqual(updated["accessPoint"], "imDisplay-1234")
            self.assertEqual(updated["address"], "192.168.1.50")
            self.assertTrue(updated["ready"])
            self.assertTrue(updated["accessPointReady"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    @mock.patch.object(lan_control.time, "sleep")
    @mock.patch.object(lan_control, "read_state")
    @mock.patch.object(lan_control, "post_query")
    def test_reboot_waits_for_new_boot_identity(self, post_query, read_state, _sleep):
        read_state.side_effect = [{"bootId": 7}, {"bootId": 8}]
        state = lan_control.reboot_and_wait("http://192.168.1.50", "secret", 7)
        self.assertEqual(state["bootId"], 8)
        post_query.assert_called_once_with(
            "http://192.168.1.50", "secret", "/api/reboot", {}, expected_status=202
        )


if __name__ == "__main__":
    unittest.main()
