# imDisplay firmware

PlatformIO firmware for the X-SURE V1.C ESP32-WROOM-32D hardware described in
the repository README. It provides a full-raster 320x240 launcher, built-in applets,
persistent display/LED/sound/timer settings, authenticated LAN control, and OTA
recovery. The ST7789 retains unchanged pixels: normal quota delivery does not
trigger a periodic repaint, and fresh status has no changing age label.
Overview fills the usable screen with 107-pixel quota digits, a wide progress
bar, reset strip, and navigation footer. Applet and settings screens use
full-width focus rows with stable typography instead of small side-by-side
cards. Applets, LED settings, sound settings, and display settings expose a
visible one-click Back action; double-click and hold remain global fallbacks.

Full pages are composed in the same 4-bit mirror used by authenticated screen
capture, then transferred to the LCD in one complete raster transaction. Menu
selection and running-timer changes retain their smaller partial updates. This
avoids full-page overdraw without changing any of the 76,800 rendered pixels.

Codex quota normally uses protocol-v1 device pull from the Mac's launchd
socket-activated cache responder. The authenticated `/api/budget-source`
configuration stores only a strict lowercase single-label `.local` Mac hostname
(or compatible private IPv4), fixed port, and a separate 256-bit read-only key;
`/api/state` reads back configuration and pull counters without the key. For a
`.local` host, the budget worker strips the suffix and performs a bounded
1-second ESPmDNS query. It accepts only a private IPv4 answer and caches that
answer in RAM for five minutes; resolution never writes flash. Pulls run off the
render/input loop, start when station Wi-Fi connects or a Codex page is entered,
remain at least 60 seconds apart, and back off to at most five minutes. Signed
no-data/stale responses preserve last-good quota until its source age becomes
stale, and unchanged fresh values do not repaint.

The transport is local plaintext HTTP with nonce-bound HMAC-SHA256, not TLS.
Confidentiality relies on encrypted private Wi-Fi; HMAC supplies endpoint/data
authenticity and integrity. A spoofed mDNS answer can redirect a request or deny
service, but cannot forge an accepted quota response without the separate HMAC
key. Legacy authenticated `POST /api/budget` and UART budget delivery default
off and are recovery-only. Full setup and threat-model details are in
`../mac-bridge/PULL_PROTOCOL.md`.

Build from the repository root:

```bash
python3 -m platformio run -d firmware
```

The custom partition table preserves the factory geometry. Initial ROM recovery
uses these segments; never use PlatformIO's generic upload action:

| Address | Artifact |
| ---: | --- |
| `0x1000` | `.pio/build/xsure/bootloader.bin` |
| `0x8000` | `.pio/build/xsure/partitions.bin` |
| `0xd000` | framework `tools/partitions/boot_app0.bin` |
| `0x10000` | `.pio/build/xsure/firmware.bin` |

Routine releases use authenticated LAN OTA, device-side exact-size/SHA-256
verification, exact version readback, the on-device GPIO5 self-test, and a
validated 320x240 screen capture:

```bash
python3 scripts/wifi_ota.py \
  firmware/releases/VERSION/firmware.bin \
  --credential xsure-backup/runtime-credential.json \
  --address-file xsure-backup/lan-state.json \
  --expect-version VERSION --wait-seconds 10
```

Use `--skip-self-test` or `--skip-screen-readback` only when restoring legacy
recovery firmware that lacks the corresponding authenticated endpoint. Routine
OTA also requires the receiver's SHA-256 verification readback; use
`--allow-legacy-unverified` only for an explicitly identified legacy recovery
receiver. A standalone capture is available with:

```bash
python3 scripts/screen_capture.py \
  --credential xsure-backup/runtime-credential.json \
  --address-file xsure-backup/lan-state.json \
  --output xsure-backup/latest-screen.bmp
```

`GET /api/screen.bmp` streams the authenticated 4-bit same-renderer framebuffer:
320x240, 76,800 validated pixels, 38,400 pixel bytes. It proves the exact frame
firmware sent, not the LCD glass/electrical path.

Recovery Wi-Fi is persistently off by default. Enable `imDisplay-...` at
`192.168.4.1` explicitly from the launcher or authenticated LAN settings;
legacy HTTP username `xsure` is kept so existing private credentials continue
to work while the AP is off. The documented board's onboard USB-C is power-only;
use a separate CP2102/CH340-style 3.3 V UART adapter for data and programming.
Other ESP32 boards may have native USB data and should follow their board-specific
flashing method. Never connect adapter power and never unplug during OTA.

Single-click selects or acts after a 550 ms double-click window. A second press
that begins inside that window is latched until release, so its first click can
never activate a launcher item while the second click is still held.
Double-click goes back to the launcher (or closes it), and long-press also opens
it. Turning from Overview, Connection, or About and pausing 1.5 seconds remains
the no-button fallback. The authenticated web UI provides exact virtual turn,
press, back, and hold controls plus logical screen readback. Encoder and GPIO5
button edges are interrupt-latched. GPIO5 keeps a bounded timestamped edge
queue, so a complete click remains visible even when a full-frame transfer
temporarily blocks the main loop; the same 8 ms debounce filters contact bounce.
`/api/self-test?input=1` drives the electrical GPIO5 path and must prove a
complete queued click with no polling during the gesture, all nine page/launcher
round trips, all four visible one-click Back actions, long-press navigation,
second-press grace, exact counters, and restoration of the prior screen. This
semantic result is mandatory in routine OTA and LAN gates. Rapid
setting changes are persisted in one coalesced
NVS session with only the changed legacy-compatible keys written. Authenticated
state reports the pending mask, session count, key-write attempts, and failures
for live verification. Display backlight is adjustable from 10-100%; sound offers
Mute, Minimal, and Soft profiles; LEDs offer presets,
brightness, and optional event glows (default Warm/20%). The same settings are
available in the authenticated local web UI and persist across reboot.

The directly attached owner UART emits one credential-capture line even while
recovery Wi-Fi is off. `scripts/capture_runtime_credential.py` stores that line
in an ignored owner-only file; the password is never exposed over unauthenticated
LAN endpoints.
