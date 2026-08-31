# imDisplay Mac quota bridge

The bridge lets imDisplay read a privacy-minimal Codex quota cache without
placing OpenAI credentials, browser state, prompts, or account identifiers on
the device. It is optional; every other imDisplay feature works without it.

## 1. Produce the owner-only cache

`codex_quota_cache_producer.py` is a bounded Codex hook. It validates that the
provided transcript is an owner-controlled regular JSONL file below
`~/.codex/sessions`, reads at most the final 2 MiB, extracts only the newest valid
`token_count` rate-limit event, and atomically writes:

```text
~/Library/Caches/imDisplay/codex-budget.json
```

The directory is mode `0700`; the cache and lock are `0600`. The producer has no
network, subprocess, or executable-launch path. It coalesces `PostToolUse` writes
for 60 seconds and performs a final bounded attempt on `Stop`.

Copy the producer to a stable owner-only path, compute its SHA-256, and add these
two groups to the existing `hooks` object in `~/.codex/hooks.json`. Replace both
placeholders with the absolute installed path and its lowercase SHA-256; preserve
all pre-existing hook groups.

```json
{
  "PostToolUse": [{
    "matcher": ".*",
    "hooks": [{
      "type": "command",
      "command": "/usr/bin/python3 -I -S __ABSOLUTE_PRODUCER_PATH__ --expected-self-sha __SHA256__ hook --event post-tool-use",
      "timeout": 3,
      "async": true
    }]
  }],
  "Stop": [{
    "hooks": [{
      "type": "command",
      "command": "/usr/bin/python3 -I -S __ABSOLUTE_PRODUCER_PATH__ --expected-self-sha __SHA256__ hook --event stop",
      "timeout": 3
    }]
  }]
}
```

Hook configuration and trust handling can change between Codex versions. Review
the current Codex configuration guidance, approve only the exact installed
command/hash, and verify a fresh owner-only cache before continuing. Do not copy
another machine's trust-state entries.

## 2. Prepare the responder and device configuration

Choose a stable lowercase single-label `.local` hostname and the Mac's current
private IPv4 address. The hostname is provisioned to imDisplay; the IPv4 address
is the exact listener bind. If DHCP later changes that address, re-render and
re-bootstrap the LaunchAgent rather than falling back to a wildcard bind.

Validate the package, create the separate 256-bit read-only HMAC key, and create
an owner-only provisioning file outside the repository:

```bash
/usr/bin/python3 -I -S mac-bridge/manage_cache_responder.py check-only
/usr/bin/python3 -I -S mac-bridge/manage_cache_responder.py prepare-key \
  --mac-host imdisplay-mac.local \
  --provisioning-output /private/path/imdisplay-budget-source.json
```

Provision that JSON once through the authenticated device endpoint:

```bash
python3 scripts/lan_control.py \
  --budget-source-config /private/path/imdisplay-budget-source.json \
  --credential /private/path/device-credential.json \
  --address-file /private/path/device-address.json
```

Install an immutable versioned responder and render the LaunchAgent, but do not
bootstrap it yet:

```bash
/usr/bin/python3 -I -S mac-bridge/manage_cache_responder.py install \
  --mac-host imdisplay-mac.local \
  --listen-address 192.168.1.10
```

`192.168.1.10` is an example private address, not a default. The manager refuses
wildcard, loopback, public, unsafe-owner, mutable, linked, oversized, or existing
targets. It never invokes launchctl or a shell.

## 3. Activate and verify

After inspecting the rendered plist and paths, explicitly bootstrap the job:

```bash
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/local.imdisplay.cache-responder.plist"
```

launchd owns the exact-address listener. `Wait=true` passes that listener to one
bounded Python process. The responder accepts at most eight connections, gives
each request two seconds, exits after three idle seconds or twenty total seconds,
and launchd throttles relaunches to at most one per ten seconds. No responder is
resident between bursts.

Verify a fresh device pull, unchanged-value no-repaint behavior, explicit stale
and no-data states, disconnect/reconnect backoff, and login/restart activation.
To disable, first boot out the exact job, then recoverably rename its plist:

```bash
launchctl bootout "gui/$(id -u)/local.imdisplay.cache-responder"
/usr/bin/python3 -I -S mac-bridge/manage_cache_responder.py rollback
```

For a previous immutable nowait installation, `stage-upgrade` validates the
previous responder/plist and writes separate v3 artifacts. The operator must
then perform an explicit bootout, recoverable old-plist rename, staged-plist
rename, and bootstrap. See [PULL_PROTOCOL.md](PULL_PROTOCOL.md) for the exact
security invariants and migration boundary.
