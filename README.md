# imDisplay for ESP32-WROOM-32D

imDisplay is replacement firmware for the X-SURE V1.C desktop display. It turns
the original ESP32-WROOM-32D hardware into a 320x240 applet display with a Codex
quota view, work timer, encoder navigation, LEDs, sound, authenticated LAN
control, and verified Wi-Fi OTA updates.

On boards with a working encoder push switch, push selects and hold goes back.
A quick right-left or left-right turn provides the same actions when that switch
is absent or faulty; the on-device hints show both control paths.

Released under the [MIT License](LICENSE).

## Hardware

- X-SURE V1.C board with ESP32-WROOM-32D, ST7789 display, encoder, four RGB LEDs,
  and the original speaker path.
- A 5 V USB-C power source.
- For initial programming or emergency recovery, a separate 3.3 V USB-to-UART
  adapter such as a CP2102 or CH340. The documented board's USB-C connector is
  **power-only**; it does not carry USB data. Other ESP32 boards may provide
  native USB data and should use their board-specific flashing method.
- A private Wi-Fi network. A Mac is required only for the optional Codex quota
  bridge.

Never connect the USB-to-UART adapter's VCC/3V3/5V pin while the board is powered
through USB-C. Connect only TX, RX, and GND. Before the first write, preserve two
matching 16 MiB factory flash reads and an eFuse report outside the repository.
Routine work must not erase the whole flash.

## Build

Install Python 3.9+, PlatformIO Core, and this project's locked PlatformIO
dependencies, then run from the repository root:

```bash
python3 -m platformio run -d firmware
```

The custom partition table preserves the two OTA slots. Initial ROM recovery
uses the generated bootloader at `0x1000`, partition table at `0x8000`, framework
`boot_app0.bin` at `0xd000`, and application at `0x10000`. Use
`scripts/install_custom_firmware.py` with a private write-gate that pins the
factory backups, target board MAC, security state, artifact hashes, and those
four addresses. Do not use PlatformIO's generic upload command for this board.

After first boot, capture the random owner credential over the directly attached
UART, configure local Wi-Fi, and keep the resulting JSON files owner-only and
untracked. Routine updates use `scripts/wifi_ota.py`; it requires firmware
size/SHA-256 readback, booted version, the on-device input test, and an
authenticated 320x240 screen capture. See [firmware/README.md](firmware/README.md)
for the exact recovery, provisioning, OTA, and validation flow.

## Codex quota bridge

The optional Mac bridge has three deliberately separated parts:

1. `codex_quota_cache_producer.py` extracts only bounded rate-limit fields from
   Codex hook transcripts and atomically writes an owner-only cache.
2. launchd owns one TCP listener bound to an explicit private IPv4 address. One
   short-lived `/usr/bin/python3 -I -S` responder accepts a bounded burst, checks
   peer/local addresses, request HMAC, file identities, and freshness, then exits.
3. imDisplay pulls no faster than once per minute and verifies nonce, body hash,
   and response HMAC before parsing JSON. Missing or stale data is never shown as
   zero and never replaces a still-valid last-good value.

The responder never launches Codex, a shell, `uv`, or a subprocess. The HMAC key
is distinct from the device-control password and from OpenAI credentials. The
transport is authenticated plaintext on private Wi-Fi, not TLS; do not expose it
to the internet or an untrusted LAN. Setup and threat-model details are in
[mac-bridge/README.md](mac-bridge/README.md) and
[mac-bridge/PULL_PROTOCOL.md](mac-bridge/PULL_PROTOCOL.md).

## Security and privacy

- Recovery Wi-Fi is off by default; normal control and updates use authenticated
  LAN endpoints.
- Factory dumps, eFuses, Wi-Fi data, device credentials, HMAC keys, provisioning
  output, firmware binaries, captures, and local Codex state must remain private
  and are ignored by Git.
- Authenticated framebuffer capture proves the renderer's 76,800 pixels, not the
  physical LCD path. The automated GPIO test proves electrical/semantic input,
  not mechanical encoder feel. LED appearance and sound quality also require
  physical inspection.
- Legacy `xsure` protocol, NVS, and authentication names remain only for
  compatibility. The product name is imDisplay.

## Contributing

Keep changes small and testable. Add focused tests for changed behavior, run one
complete gate before delivery, and never commit generated firmware or private
device evidence. Hardware claims must include physical imDisplay proof; software
simulation or framebuffer evidence alone is not enough.
