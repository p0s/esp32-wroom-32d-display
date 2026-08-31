# imDisplay quota pull protocol v1

## Boundaries

imDisplay connects only to a configured lowercase single-label `.local` Mac
hostname or an RFC1918 IPv4 address. `.local` resolution is bounded to one
second, accepts only RFC1918 A records, caches the result in RAM for five
minutes, and never writes the resolved address to flash.

launchd binds the responder socket to one explicit RFC1918 address. It does not
use `0.0.0.0`, a loopback address, or a public interface. With
`inetdCompatibility.Wait=true`, the listening socket is inherited on standard
input by one exact versioned `/usr/bin/python3 -I -S` process. The responder
verifies its own SHA-256 and metadata before accepting a bounded request burst.

The cache producer and responder share only the owner-only atomic cache. The
responder never invokes the producer, Codex, a shell, `uv`, another executable,
or any network client. Its only network operation is `accept()` on launchd's
inherited listener.

## Cache

The cache path defaults to:

```text
~/Library/Caches/imDisplay/codex-budget.json
```

Its compact schema is:

```json
{"schemaVersion":1,"checkedAt":1700000000,"windows":[{"limitId":"codex","limitName":"Codex","remainingPercent":50.0,"resetsAt":1700003600,"usedPercent":50.0,"window":"primary"}]}
```

The producer requires an owner-only regular transcript below
`~/.codex/sessions`, scans only a bounded tail, recognizes a strict duplicate-free
JSON schema, and persists none of the prompt, response, plan, credit balance,
account, or usage-detail fields. Atomic rename, directory fsync, a nonblocking
owner-only lock, monotonic event timestamps, and readback prevent partial or
regressive cache updates.

The responder independently revalidates parent/file owner, type, mode, link
count, size, no-follow open, pre/post identity, JSON schema, timestamps, and a
maximum 150-second source age. Missing or invalid cache data produces signed
`No data`; old data produces signed `Stale data`. Neither carries a fabricated
percentage.

## Authenticated request

The device generates a fresh 16-byte nonce encoded as 32 lowercase hexadecimal
characters. It sends only:

```text
GET /v1/quota HTTP/1.1
Host: <configured-host>:<configured-port>
Connection: close
X-imDisplay-Protocol: 1
X-imDisplay-Nonce: <nonce>
X-imDisplay-Authorization: <request-hmac>
```

The request HMAC is HMAC-SHA256 with the separate 256-bit read-only key over:

```text
imdisplay-cache-v1
request
GET
/v1/quota
<nonce>
```

The responder rejects non-ASCII input, extra or duplicate headers, bodies,
pipelining, invalid host/nonce/HMAC, input over 2 KiB, a non-IPv4 stream, a
connected descriptor where a listener is expected, a bind that differs from
configuration, or accepted local/peer endpoints outside RFC1918. Every failure
gets the same empty 403 response.

## Authenticated response

A valid response contains at most 2,047 body bytes. It carries the original
nonce, exact content length, body SHA-256, and HMAC-SHA256 over:

```text
imdisplay-cache-v1
response
200
<nonce>
<body-sha256>
```

Firmware verifies the status and exact header set, outstanding nonce, content
length, body hash, response HMAC, and strict JSON before accepting quota data.
A captured response cannot satisfy a future nonce. Pulls remain at least 60
seconds apart with bounded failure backoff; unchanged fresh content does not
repaint the display.

## Process and denial-of-service bounds

- One launchd listener on one explicit private address; no wildcard bind.
- One responder process for a bounded burst, not one process per connection.
- At most eight accepted connections per process.
- Two seconds per request, three seconds idle, twenty seconds total lifetime.
- 16 KiB cache, 2 KiB request, 2,047-byte body, and at most six windows.
- launchd CPU/core/file-descriptor limits and a ten-second relaunch throttle.
- `/usr/bin/python3 -I -S` ignores user Python environment and site packages.

These controls contain process-spawn and parser cost. A private-LAN peer can
still deny access by consuming the bounded slots, but cannot read quota or forge
an accepted body without the HMAC key. The device's 60-second minimum poll means
normal operation does not create a connection storm.

## Threat model

| Threat | Control and remaining assumption |
| --- | --- |
| LAN forgery or response replay | Fresh nonce, request/response HMAC, exact body hash, constant-time comparison. Key compromise remains decisive. |
| mDNS spoofing | Only RFC1918 answers are accepted; spoofing can redirect or deny but cannot forge HMAC. |
| Process-spawn flood | `Wait=true`, one bounded listener process, connection/lifetime limits, and launchd throttle. LAN denial remains possible. |
| Local cache/key swap | Owner/type/mode/link/size/no-follow/identity checks and strict JSON. Same-owner Mac compromise is out of scope. |
| Mutable executable | Immutable versioned path, exact self SHA-256, no group/world write, no dynamic import or executable launch. |
| Credential expansion | The bridge key grants quota read/forgery only. Device control and OpenAI credentials are separate and never enter cache or firmware configuration. |

The protocol does not provide TLS. Confidentiality relies on encrypted private
Wi-Fi, while HMAC provides authenticity and integrity. Do not expose the socket
to the internet, port-forward it, or silently treat RFC1918 as sufficient trust.

## Activation and upgrade boundary

`manage_cache_responder.py` can validate, install, stage, or recoverably disable
artifacts, but never invokes launchctl. A clean `install` writes an immutable v3
responder and exact-address plist. `stage-upgrade` first validates an immutable
previous nowait responder/plist and writes separate v3 files without altering
the running job.

Activation is always an explicit operator action: inspect paths and hashes,
boot out the previous exact label when applicable, rename the old plist to a
recoverable disabled name, rename the staged plist into place, and bootstrap the
new exact plist. Each mutation should be joined so failure stops the chain.
Rollback first boots out v3, then renames its validated plist to `.disabled`;
versioned responders and keys remain available for inspection.
