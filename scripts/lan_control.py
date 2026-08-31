#!/usr/bin/env python3
"""Verify authenticated imDisplay control through its private LAN address."""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from wifi_ota import load_credential, load_private_address
from screen_capture import capture_screen


PAGES = (
    "overview",
    "windows",
    "timer",
    "applets",
    "leds",
    "display",
    "sounds",
    "connection",
    "about",
    "menu",
)
INPUT_ACTIONS = ("press", "back", "hold", "clockwise", "counterclockwise")
MAX_BUDGET_SOURCE_CONFIG_BYTES = 512
LOCAL_HOSTNAME = re.compile(r"(?=.{7,63}\Z)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.local\Z")
LOWER_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
SAFE_STATE_FIELDS = (
    "product",
    "firmware",
    "bootId",
    "page",
    "menu",
    "received",
    "valid",
    "ageSeconds",
    "remaining",
    "reset",
    "ota",
    "freeHeap",
    "minFreeHeap",
    "stationConfigured",
    "stationConnected",
    "accessPointEnabled",
    "accessPointReady",
    "applets",
    "leds",
    "display",
    "render",
    "persistence",
    "sound",
    "timer",
    "input",
    "selfTest",
    "screen",
)
SAFE_BUDGET_PULL_FIELDS = (
    "protocol",
    "configured",
    "enabled",
    "legacyPushEnabled",
    "workerReady",
    "inFlight",
    "attempts",
    "successes",
    "failures",
    "consecutiveFailures",
    "lastResult",
    "lastResultAgeSeconds",
    "transport",
    "discovery",
    "workerStackHighWater",
)


def authenticated_request(url: str, password: str, method: str = "GET") -> urllib.request.Request:
    token = base64.b64encode(f"xsure:{password}".encode("utf-8")).decode("ascii")
    return urllib.request.Request(
        url,
        data=b"" if method == "POST" else None,
        method=method,
        headers={"Authorization": f"Basic {token}", "Cache-Control": "no-store"},
    )


def open_request(
    request: urllib.request.Request, timeout: float = 10
) -> tuple[int, bytes]:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def read_state(base_url: str, password: str) -> dict[str, object]:
    status, body = open_request(authenticated_request(f"{base_url}/api/state", password))
    if status != 200:
        raise RuntimeError(f"device state returned HTTP {status}")
    state = json.loads(body)
    if not isinstance(state, dict):
        raise RuntimeError("device state was not a JSON object")
    return state


def safe_state(state: dict[str, object]) -> dict[str, object]:
    result = {key: state[key] for key in SAFE_STATE_FIELDS if key in state}
    pull = state.get("budgetPull")
    if isinstance(pull, dict):
        result["budgetPull"] = {
            key: pull[key] for key in SAFE_BUDGET_PULL_FIELDS if key in pull
        }
    return result


def verify_device_address(
    state: dict[str, object], expected_address: str
) -> dict[str, object]:
    if state.get("stationConnected") is not True or state.get("stationIp") != expected_address:
        raise RuntimeError("authenticated device address readback did not match")
    return state


def post_query(
    base_url: str,
    password: str,
    path: str,
    values: dict[str, object],
    expected_status: int = 204,
) -> None:
    query = urllib.parse.urlencode(values)
    status, _ = open_request(
        authenticated_request(f"{base_url}{path}?{query}", password, "POST")
    )
    if status != expected_status:
        raise RuntimeError(f"imDisplay action returned HTTP {status}")


def valid_pull_host(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if LOCAL_HOSTNAME.fullmatch(value):
        return True
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return isinstance(address, ipaddress.IPv4Address) and any(
        address in network for network in PRIVATE_NETWORKS
    )


def load_budget_source_config(path: Path) -> dict[str, object]:
    parent = os.lstat(path.parent)
    if (
        stat.S_IFMT(parent.st_mode) != stat.S_IFDIR
        or parent.st_uid != os.geteuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
    ):
        raise RuntimeError("budget source directory is not owner-only")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags)
    try:
        before = os.fstat(descriptor)
        if (
            stat.S_IFMT(before.st_mode) != stat.S_IFREG
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or not 0 < before.st_size <= MAX_BUDGET_SOURCE_CONFIG_BYTES
        ):
            raise RuntimeError("budget source configuration is not owner-only")
        data = os.read(descriptor, MAX_BUDGET_SOURCE_CONFIG_BYTES + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        len(data) != before.st_size
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise RuntimeError("budget source configuration changed during read")
    try:
        config = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("invalid budget source configuration") from error
    expected = {
        "schema",
        "kind",
        "enabled",
        "macHost",
        "macPort",
        "readOnlyKey",
        "legacyPushEnabled",
    }
    host = config.get("macHost") if isinstance(config, dict) else None
    key = config.get("readOnlyKey") if isinstance(config, dict) else None
    if (
        not isinstance(config, dict)
        or set(config) != expected
        or config.get("schema") != 1
        or config.get("kind") != "budget_pull_config"
        or config.get("enabled") is not True
        or config.get("legacyPushEnabled") is not False
        or not valid_pull_host(host)
        or isinstance(config.get("macPort"), bool)
        or not isinstance(config.get("macPort"), int)
        or not 1024 <= config["macPort"] <= 65535
        or not isinstance(key, str)
        or not LOWER_SHA256.fullmatch(key)
    ):
        raise RuntimeError("invalid budget source configuration")
    return config


def provision_budget_source(
    base_url: str, password: str, config_path: Path
) -> dict[str, object]:
    config = load_budget_source_config(config_path)
    body = json.dumps(config, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    token = base64.b64encode(f"xsure:{password}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        f"{base_url}/api/budget-source",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Basic {token}",
            "Cache-Control": "no-store",
            "Content-Type": "application/json",
        },
    )
    status, _ = open_request(request)
    if status != 204:
        raise RuntimeError(f"budget source provisioning returned HTTP {status}")
    state = read_state(base_url, password)
    pull = state.get("budgetPull")
    if (
        not isinstance(pull, dict)
        or pull.get("configured") is not True
        or pull.get("enabled") is not True
        or pull.get("legacyPushEnabled") is not False
        or pull.get("macHost") != config["macHost"]
        or pull.get("macPort") != config["macPort"]
    ):
        raise RuntimeError("budget source configuration readback did not match")
    return state


def select_page(base_url: str, password: str, page: str) -> dict[str, object]:
    post_query(base_url, password, "/api/control", {"page": page})
    time.sleep(0.15)
    state = read_state(base_url, password)
    if page == "menu":
        if state.get("menu") is not True:
            raise RuntimeError("device did not enter the menu")
    elif state.get("page") != page.upper() or state.get("menu") is not False:
        raise RuntimeError(f"device did not select {page}")
    return state


def send_input(base_url: str, password: str, action: str) -> dict[str, object]:
    if action not in INPUT_ACTIONS:
        raise ValueError(f"unknown virtual knob action: {action}")
    before = read_state(base_url, password)
    previous = before.get("input", {}).get("remoteEvents", 0)
    post_query(base_url, password, "/api/input", {"action": action})
    time.sleep(0.15)
    state = read_state(base_url, password)
    input_state = state.get("input", {})
    if input_state.get("remoteEvents") != previous + 1:
        raise RuntimeError("device did not acknowledge the virtual knob action")
    if input_state.get("lastRemoteAction") != action.upper():
        raise RuntimeError("device virtual knob readback did not match")
    return state


def run_input_self_test(base_url: str, password: str) -> dict[str, object]:
    status, body = open_request(
        authenticated_request(
            f"{base_url}/api/self-test?input=1", password, "POST"
        ),
        timeout=35,
    )
    if status != 200:
        raise RuntimeError(f"input self-test returned HTTP {status}")
    result = json.loads(body)
    if not isinstance(result, dict) or result.get("pass") is not True:
        raise RuntimeError("input self-test did not pass")
    expected = {
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
    if any(result.get(key) != value for key, value in expected.items()):
        raise RuntimeError("input/navigation self-test evidence did not match")
    state = read_state(base_url, password)
    self_test = state.get("selfTest", {})
    if (
        self_test.get("schema") != 4
        or self_test.get("inputPassed") is not True
        or self_test.get("navigationPassed") is not True
        or self_test.get("pageRoundTrips") != 9
        or self_test.get("pageRoundTripsExpected") != 9
        or self_test.get("backActions") != 4
        or self_test.get("backActionsExpected") != 4
        or self_test.get("longPressBack") is not True
        or self_test.get("doubleClickFallback") is not True
        or self_test.get("queuedPulse") is not True
        or self_test.get("edgeQueueHealthy") is not True
        or self_test.get("stateRestored") is not True
        or self_test.get("failure") != "NONE"
        or self_test.get("failurePage") != "NONE"
    ):
        raise RuntimeError("input/navigation self-test state readback did not pass")
    return state


def set_leds(
    base_url: str,
    password: str,
    preset: str | None = None,
    brightness: int | None = None,
) -> dict[str, object]:
    values: dict[str, object] = {}
    if preset is not None:
        values["preset"] = preset
    if brightness is not None:
        values["brightness"] = brightness
    if not values:
        raise ValueError("provide an LED preset or brightness")
    post_query(base_url, password, "/api/leds", values)
    state = read_state(base_url, password)
    leds = state.get("leds", {})
    if preset is not None and leds.get("preset") != preset.upper():
        raise RuntimeError("LED preset readback did not match")
    if brightness is not None and leds.get("brightness") != brightness:
        raise RuntimeError("LED brightness readback did not match")
    return state


def set_recovery_wifi(
    base_url: str, password: str, enabled: bool, timeout_seconds: float = 4
) -> dict[str, object]:
    post_query(
        base_url,
        password,
        "/api/recovery-wifi",
        {"enabled": int(enabled)},
        expected_status=202,
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = read_state(base_url, password)
        if (
            state.get("accessPointEnabled") is enabled
            and state.get("accessPointReady") is enabled
        ):
            return state
        time.sleep(0.1)
    raise TimeoutError("recovery Wi-Fi setting did not reach the requested state")


def applet_installed(state: dict[str, object], applet_id: str) -> bool:
    for applet in state.get("applets", []):
        if applet.get("id") == applet_id:
            return applet.get("installed") is True
    raise RuntimeError(f"missing applet state: {applet_id}")


def persistent_settings(state: dict[str, object]) -> dict[str, object]:
    return {
        "applets": {
            applet_id: applet_installed(state, applet_id)
            for applet_id in ("codex", "timer")
        },
        "leds": {
            key: state["leds"][key]
            for key in ("preset", "brightness", "feedback")
        },
        "displayBrightness": state["display"]["brightness"],
        "sound": {
            key: state["sound"][key] for key in ("profile", "volume")
        },
        "timerMinutes": state["timer"]["durationMinutes"],
        "recoveryWifi": state.get("accessPointEnabled") is True,
    }


def sync_credential_metadata(path: Path, address: str, state: dict[str, object]) -> None:
    access_point = state.get("accessPoint")
    if not isinstance(access_point, str) or not access_point:
        raise RuntimeError("device state did not identify its recovery access point")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["accessPoint"] = access_point
    raw["address"] = address
    raw["accessPointReady"] = state.get("accessPointReady") is True
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def reboot_and_wait(
    base_url: str, password: str, previous_boot_id: int, timeout_seconds: int = 45
) -> dict[str, object]:
    post_query(base_url, password, "/api/reboot", {}, expected_status=202)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            state = read_state(base_url, password)
            if state.get("bootId") != previous_boot_id:
                return state
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
            pass
        time.sleep(1)
    raise TimeoutError("imDisplay did not return with a new boot identity")


def verify_all(credential_path: Path, address_path: Path) -> None:
    credential = load_credential(credential_path)
    address = load_private_address(address_path)
    base_url = f"http://{address}"
    initial = verify_device_address(
        read_state(base_url, credential["password"]), address
    )
    if initial.get("product") != "imDisplay":
        raise RuntimeError("LAN endpoint did not identify as imDisplay")
    if not str(initial.get("accessPoint", "")).startswith("imDisplay-"):
        raise RuntimeError("recovery access point did not use imDisplay casing")
    if initial.get("accessPointEnabled") is not False or initial.get(
        "accessPointReady"
    ) is not False:
        raise RuntimeError("recovery Wi-Fi was not off by default")
    initial_display = initial.get("display", {})
    if (
        initial_display.get("width") != 320
        or initial_display.get("height") != 240
        or initial_display.get("pixels") != 76800
    ):
        raise RuntimeError("device did not report the full 320x240 native raster")
    original_persistent_settings = persistent_settings(initial)
    run_input_self_test(base_url, credential["password"])
    print(
        "verified on-device GPIO5 gesture/navigation self-test: "
        "9 page round trips and 4 one-click Back actions",
        flush=True,
    )
    screen_report = capture_screen(
        address, credential["password"], credential_path.parent / "latest-screen.bmp"
    )
    print(
        f"verified authenticated {screen_report['width']}x{screen_report['height']} "
        "pixel mirror",
        flush=True,
    )
    sync_credential_metadata(credential_path, address, initial)
    set_recovery_wifi(base_url, credential["password"], True)
    set_recovery_wifi(base_url, credential["password"], False)
    print("verified recovery Wi-Fi explicit on/off control and final off", flush=True)
    original_applets = {
        applet_id: applet_installed(initial, applet_id) for applet_id in ("codex", "timer")
    }
    original_leds = initial["leds"]
    original_display_brightness = initial_display["brightness"]
    original_sound = initial["sound"]
    original_timer_minutes = initial["timer"]["durationMinutes"]
    post_query(base_url, credential["password"], "/api/applets", {"id": "codex", "installed": 1})
    post_query(base_url, credential["password"], "/api/applets", {"id": "timer", "installed": 1})
    select_page(base_url, credential["password"], "overview")
    turned = send_input(base_url, credential["password"], "clockwise")
    screen = turned.get("screen", {})
    if screen.get("mode") != "MENU" or screen.get("selection") != "WINDOWS":
        raise RuntimeError("virtual knob did not move from Overview to Windows")
    pressed = send_input(base_url, credential["password"], "press")
    if pressed.get("page") != "WINDOWS" or pressed.get("menu") is not False:
        raise RuntimeError("virtual knob press did not open Windows")
    backed = send_input(base_url, credential["password"], "back")
    if backed.get("page") != "WINDOWS" or backed.get("menu") is not True:
        raise RuntimeError("virtual back did not open the launcher")
    closed = send_input(base_url, credential["password"], "back")
    if closed.get("page") != "WINDOWS" or closed.get("menu") is not False:
        raise RuntimeError("virtual back did not close the launcher")
    print("verified logical screen mirror and virtual turn/press/back", flush=True)
    for page in PAGES:
        state = select_page(base_url, credential["password"], page)
        print(
            f"verified LAN control={page} firmware={state.get('firmware')} "
            f"budget={int(bool(state.get('received')))}",
            flush=True,
        )
    post_query(base_url, credential["password"], "/api/applets", {"id": "timer", "installed": 0})
    if applet_installed(read_state(base_url, credential["password"]), "timer"):
        raise RuntimeError("Work Timer applet did not uninstall")
    post_query(base_url, credential["password"], "/api/applets", {"id": "timer", "installed": 1})
    if not applet_installed(read_state(base_url, credential["password"]), "timer"):
        raise RuntimeError("Work Timer applet did not reinstall")
    print("verified applet uninstall/reinstall=timer", flush=True)

    post_query(
        base_url,
        credential["password"],
        "/api/leds",
        {"preset": "rainbow", "brightness": 20},
    )
    changed = read_state(base_url, credential["password"])["leds"]
    if changed.get("preset") != "RAINBOW" or changed.get("brightness") != 20:
        raise RuntimeError("LED settings did not change")
    post_query(
        base_url,
        credential["password"],
        "/api/leds",
        {"preset": original_leds["preset"], "brightness": original_leds["brightness"]},
    )
    print("verified LED settings and restore", flush=True)

    test_display_brightness = 70 if original_display_brightness != 70 else 80
    post_query(
        base_url,
        credential["password"],
        "/api/display",
        {"brightness": test_display_brightness},
    )
    changed_display = read_state(base_url, credential["password"])["display"]
    duty = changed_display.get("backlightDuty", 0)
    if changed_display.get("brightness") != test_display_brightness or not 1 <= duty <= 255:
        raise RuntimeError("display brightness did not change through the hardware PWM setting")
    post_query(
        base_url,
        credential["password"],
        "/api/display",
        {"brightness": original_display_brightness},
    )
    print("verified full native raster and display dimming restore", flush=True)

    post_query(
        base_url,
        credential["password"],
        "/api/sound",
        {"profile": "soft", "volume": 20, "test": 1},
    )
    sound_deadline = time.monotonic() + 3
    changed_sound = {}
    while time.monotonic() < sound_deadline:
        changed_sound = read_state(base_url, credential["password"])["sound"]
        if changed_sound.get("driverReady") is True:
            break
        time.sleep(0.1)
    if changed_sound.get("driverReady") is not True or changed_sound.get("lastError") != 0:
        raise RuntimeError("factory-proven PDM sound driver did not become ready")
    post_query(
        base_url,
        credential["password"],
        "/api/sound",
        {"profile": original_sound["profile"], "volume": original_sound["volume"]},
    )
    print("verified sound driver/test cue request and restore", flush=True)

    post_query(
        base_url,
        credential["password"],
        "/api/timer",
        {"action": "reset", "minutes": original_timer_minutes},
    )
    post_query(base_url, credential["password"], "/api/timer", {"action": "start"})
    if read_state(base_url, credential["password"])["timer"].get("running") is not True:
        raise RuntimeError("Work Timer did not start")
    post_query(base_url, credential["password"], "/api/timer", {"action": "pause"})
    if read_state(base_url, credential["password"])["timer"].get("running") is not False:
        raise RuntimeError("Work Timer did not pause")
    print("verified Work Timer reset/start/pause", flush=True)

    persistence_minutes = 30 if original_timer_minutes != 30 else 35
    persistence_preset = "WARM" if original_leds["preset"] != "WARM" else "FOCUS"
    persistence_brightness = 40 if original_leds["brightness"] != 40 else 50
    persistence_feedback = not original_leds["feedback"]
    persistence_display_brightness = 60 if original_display_brightness != 60 else 70
    persistence_sound_profile = (
        "MINIMAL" if original_sound["profile"] != "MINIMAL" else "SOFT"
    )
    persistence_sound_volume = 30 if original_sound["volume"] != 30 else 40
    post_query(
        base_url,
        credential["password"],
        "/api/timer",
        {"action": "reset", "minutes": persistence_minutes},
    )
    post_query(
        base_url,
        credential["password"],
        "/api/leds",
        {
            "preset": persistence_preset,
            "brightness": persistence_brightness,
            "feedback": int(persistence_feedback),
        },
    )
    post_query(
        base_url,
        credential["password"],
        "/api/display",
        {"brightness": persistence_display_brightness},
    )
    post_query(
        base_url,
        credential["password"],
        "/api/sound",
        {"profile": persistence_sound_profile, "volume": persistence_sound_volume},
    )
    post_query(base_url, credential["password"], "/api/applets", {"id": "timer", "installed": 0})
    restarted = reboot_and_wait(base_url, credential["password"], initial["bootId"])
    if applet_installed(restarted, "timer"):
        raise RuntimeError("Work Timer uninstall did not persist across reboot")
    if (
        restarted["leds"].get("preset") != persistence_preset
        or restarted["leds"].get("brightness") != persistence_brightness
        or restarted["leds"].get("feedback") != persistence_feedback
    ):
        raise RuntimeError("LED settings did not persist across reboot")
    if restarted["display"].get("brightness") != persistence_display_brightness:
        raise RuntimeError("display brightness did not persist across reboot")
    if (
        restarted["sound"].get("profile") != persistence_sound_profile
        or restarted["sound"].get("volume") != persistence_sound_volume
    ):
        raise RuntimeError("sound settings did not persist across reboot")
    if restarted["timer"].get("durationMinutes") != persistence_minutes:
        raise RuntimeError("Work Timer duration did not persist across reboot")
    print(
        "verified applet, LED, display, sound, and timer persistence across LAN reboot",
        flush=True,
    )

    post_query(base_url, credential["password"], "/api/applets", {"id": "timer", "installed": 1})
    post_query(
        base_url,
        credential["password"],
        "/api/timer",
        {"action": "reset", "minutes": original_timer_minutes},
    )
    for applet_id, installed in original_applets.items():
        post_query(
            base_url,
            credential["password"],
            "/api/applets",
            {"id": applet_id, "installed": int(installed)},
        )
    post_query(
        base_url,
        credential["password"],
        "/api/leds",
        {
            "preset": original_leds["preset"],
            "brightness": original_leds["brightness"],
            "feedback": int(original_leds["feedback"]),
        },
    )
    post_query(
        base_url,
        credential["password"],
        "/api/display",
        {"brightness": original_display_brightness},
    )
    post_query(
        base_url,
        credential["password"],
        "/api/sound",
        {"profile": original_sound["profile"], "volume": original_sound["volume"]},
    )
    restored = read_state(base_url, credential["password"])
    if any(
        applet_installed(restored, applet_id) != installed
        for applet_id, installed in original_applets.items()
    ):
        raise RuntimeError("original applet state was not restored")
    if restored["leds"] != original_leds or restored["timer"].get(
        "durationMinutes"
    ) != original_timer_minutes:
        raise RuntimeError("original LED or timer settings were not restored")
    if restored["display"].get("brightness") != original_display_brightness:
        raise RuntimeError("original display brightness was not restored")
    if (
        restored["sound"].get("profile") != original_sound["profile"]
        or restored["sound"].get("volume") != original_sound["volume"]
    ):
        raise RuntimeError("original sound settings were not restored")
    if persistent_settings(restored) != original_persistent_settings:
        raise RuntimeError("original persistent settings were not fully restored")
    persistence = restored.get("persistence", {})
    if persistence.get("failures", 0) != 0:
        raise RuntimeError("device reported a settings persistence failure")
    print(
        "restored original applet, LED, display, sound, and timer settings; "
        f"persistence sessions={persistence.get('sessions', 'unknown')} "
        f"keyWrites={persistence.get('keyWriteAttempts', 'unknown')}",
        flush=True,
    )

    final = reboot_and_wait(
        base_url, credential["password"], restored["bootId"]
    )
    if persistent_settings(final) != original_persistent_settings:
        raise RuntimeError("restored settings did not persist across the final reboot")
    if final.get("accessPointEnabled") is not False or final.get(
        "accessPointReady"
    ) is not False:
        raise RuntimeError("recovery Wi-Fi was not off after the final reboot")
    print(
        "verified restored settings across a final LAN reboot and recovery Wi-Fi off",
        flush=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--credential",
        type=Path,
        default=Path("xsure-backup/runtime-credential.json"),
    )
    parser.add_argument(
        "--address-file", type=Path, default=Path("xsure-backup/lan-state.json")
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--state-only", action="store_true", help="read and print privacy-filtered state without changes"
    )
    mode.add_argument(
        "--reboot-only", action="store_true", help="reboot once and verify a new boot identity"
    )
    mode.add_argument("--page", choices=PAGES, help="show one page or the launcher")
    mode.add_argument(
        "--input-action",
        choices=INPUT_ACTIONS,
        help="simulate one exact knob action through the authenticated LAN API",
    )
    mode.add_argument(
        "--self-test",
        action="store_true",
        help="run the authenticated on-device GPIO5 input self-test",
    )
    mode.add_argument(
        "--budget-source-config",
        type=Path,
        help="provision one owner-only pull-source file and print key/endpoint-free state",
    )
    parser.add_argument(
        "--led-preset",
        choices=("off", "codex", "focus", "warm", "alert", "rainbow"),
        help="set one persistent LED preset and verify readback",
    )
    parser.add_argument(
        "--led-brightness",
        type=int,
        choices=range(0, 101),
        metavar="0..100",
        help="set persistent LED brightness and verify readback",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if (
        args.state_only
        or args.reboot_only
        or args.page is not None
        or args.input_action is not None
        or args.self_test
        or args.budget_source_config is not None
        or args.led_preset is not None
        or args.led_brightness is not None
    ):
        credential = load_credential(args.credential)
        address = load_private_address(args.address_file)
        base_url = f"http://{address}"
        state = verify_device_address(
            read_state(base_url, credential["password"]), address
        )
        if args.reboot_only:
            state = reboot_and_wait(
                base_url, credential["password"], state["bootId"]
            )
        elif args.page is not None:
            state = select_page(base_url, credential["password"], args.page)
        elif args.input_action is not None:
            state = send_input(base_url, credential["password"], args.input_action)
        elif args.self_test:
            state = run_input_self_test(base_url, credential["password"])
        elif args.budget_source_config is not None:
            state = provision_budget_source(
                base_url, credential["password"], args.budget_source_config
            )
        elif args.led_preset is not None or args.led_brightness is not None:
            state = set_leds(
                base_url,
                credential["password"],
                args.led_preset,
                args.led_brightness,
            )
        verify_device_address(state, address)
        print(json.dumps(safe_state(state), sort_keys=True))
        return 0
    verify_all(args.credential, args.address_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
