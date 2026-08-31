#!/usr/bin/python3

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MAIN = (ROOT / "firmware/src/main.cpp").read_text(encoding="utf-8")
CLIENT = (ROOT / "firmware/src/budget_pull_client.cpp").read_text(encoding="utf-8")
PROTOCOL = (ROOT / "firmware/src/budget_pull_protocol.cpp").read_text(encoding="utf-8")


def function_body(source: str, name: str) -> str:
    match = re.search(rf"\b{name}\s*\([^;]*?\)\s*\{{", source, re.DOTALL)
    if not match:
        raise AssertionError(f"function not found: {name}")
    start = match.end()
    depth = 1
    cursor = start
    while cursor < len(source) and depth:
        if source[cursor] == "{":
            depth += 1
        elif source[cursor] == "}":
            depth -= 1
        cursor += 1
    if depth:
        raise AssertionError(f"unclosed function: {name}")
    return source[start:cursor - 1]


class BudgetPullIntegrationTests(unittest.TestCase):
    def test_legacy_push_is_fail_closed_and_recovery_only(self):
        load = function_body(MAIN, "loadBudgetPullSettings")
        receive = function_body(MAIN, "receiveHttpBudget")
        serial = function_body(MAIN, "pollSerial")
        self.assertIn('getBool("push-legacy", false)', load)
        self.assertIn("if (!budgetPullSettings.legacyPushEnabled)", receive)
        self.assertIn("budgetPullSettings.legacyPushEnabled", serial)
        self.assertIn('webServer.on("/api/budget", HTTP_POST, receiveHttpBudget)', MAIN)

    def test_configuration_is_authenticated_and_key_is_not_read_back(self):
        configure = function_body(MAIN, "configureBudgetPull")
        state = function_body(MAIN, "sendState")
        self.assertIn("requireWebAuthentication", configure)
        self.assertIn('"budget_pull_config"', configure)
        self.assertIn('document["macHost"]', configure)
        self.assertIn('webServer.on("/api/budget-source", HTTP_POST, configureBudgetPull)', MAIN)
        self.assertIn('pull["macHost"]', state)
        self.assertNotIn("readOnlyKey", state)
        self.assertNotIn("pull-key", state)

    def test_poll_and_worker_never_write_flash(self):
        poll = function_body(MAIN, "pollBudgetPull")
        worker = function_body(MAIN, "budgetPullWorker")
        for body in (poll, worker, CLIENT):
            self.assertNotIn("Preferences", body)
            self.assertNotIn("preferences.", body)
            self.assertNotIn("putString", body)
            self.assertNotIn("putBool", body)

    def test_network_waits_are_off_the_render_input_loop(self):
        loop = function_body(MAIN, "loop")
        worker = function_body(MAIN, "budgetPullWorker")
        self.assertIn("pollBudgetPull();", loop)
        self.assertNotIn("performPull", loop)
        self.assertIn("performPull", worker)
        self.assertIn("xQueueReceive", worker)
        self.assertIn("xTaskCreate", function_body(MAIN, "startBudgetPullWorker"))
        self.assertIn("MDNS.queryHost", CLIENT)
        self.assertNotIn("queryHost", loop)

    def test_hostname_resolution_is_private_runtime_only_and_failure_invalidates_cache(self):
        self.assertIn("isLocalHostname(request.host)", CLIENT)
        self.assertIn("copyLocalHostnameLabel(request.host", CLIENT)
        self.assertIn("kMdnsQueryTimeoutMs = 1000", CLIENT)
        self.assertIn("WiFi.status() != WL_CONNECTED", CLIENT)
        self.assertIn("MDNS.begin", CLIENT)
        self.assertIn("MDNS.queryHost(label, kMdnsQueryTimeoutMs)", CLIENT)
        self.assertNotIn("WiFi.hostByName", CLIENT)
        self.assertNotIn("dns_gethostbyname", CLIENT)
        self.assertIn("privateEndpoint(resolved)", CLIENT)
        self.assertIn("RuntimeResolution runtimeResolution", CLIENT)
        self.assertIn("invalidateRuntimeResolution(request)", CLIENT)
        self.assertNotIn("Preferences", CLIENT)
        self.assertNotIn("putString", CLIENT)

    def test_wifi_and_codex_entry_request_immediate_but_scheduler_enforces_minute(self):
        short_press = function_body(MAIN, "shortPress")
        remote = function_body(MAIN, "remoteControl")
        loop = function_body(MAIN, "loop")
        self.assertIn("requestBudgetPull();", short_press)
        self.assertIn("requestBudgetPull();", remote)
        self.assertIn("if (stationConnected) requestBudgetPull();", loop)
        self.assertIn("kMinimumPollIntervalMs = 60000", (ROOT / "firmware/src/budget_pull_protocol.h").read_text())
        self.assertIn("schedule.notBeforeMs = now + kMinimumPollIntervalMs", PROTOCOL)

    def test_unavailable_payload_preserves_last_good_and_freshness_origin(self):
        accept = function_body(MAIN, "acceptPayload")
        unavailable = accept.index('setBudgetPullLastResult(stale ? "stale" : "no-data")')
        assignment = accept.index("budget = next")
        self.assertLess(unavailable, assignment)
        self.assertIn("return true;", accept[unavailable:assignment])
        self.assertIn("millis() - sourceAgeSeconds * 1000U", accept)
        self.assertIn("sourceAgeSeconds", accept)

    def test_plain_transport_is_not_silently_presented_as_tls(self):
        self.assertIn("WiFiClient client", CLIENT)
        self.assertNotIn("WiFiClientSecure", CLIENT)
        state = function_body(MAIN, "sendState")
        self.assertIn('pull["transport"] = "local-http+hmac-sha256"', state)


if __name__ == "__main__":
    unittest.main()
