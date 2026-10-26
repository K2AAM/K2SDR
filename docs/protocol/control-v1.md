# Control protocol v1 — notes

The machine-readable definition is `protocol/schema/control-v1.json`, generated
from `control/src/k2websdr/protocol/models.py`. This page records the rules the
schema cannot express.

## Frequencies

- Wire fields carrying a frequency or rate are JSON integers named `*_hz`.
  A test walks the generated schema and fails if any `*_hz` field admits a
  non-integer, or if a frequency-like field lacks the `_hz` suffix.
- Floats (including integral ones such as `7074000.0`), strings, booleans,
  negatives and values above 10 GHz are rejected with `E_SCHEMA`.
- Human unit strings (`"7.074 MHz"`) exist only on explicit input paths via
  `k2websdr.protocol.freq.parse_frequency`, tested by
  `protocol/vectors/frequency_input.json`. They never appear in messages.
- Channels use absolute dial `frequency_hz` (not an offset from capture centre),
  so the same integer appears in UI, state, logs and decoder events.

## Command lifecycle

1. Client sends `cmd` with a ULID `id`.
2. Server replies with exactly one `ack`: `accepted`, or `rejected` + `code`.
3. If accepted, exactly one `result` follows: `applied` or `failed` + `code`.
4. State changes arrive as `patch` messages; field `status` is `pending` with
   the command id until the worker reports `APPLIED`.

A UI shows only a field's `applied` value as in effect.

## State delivery

`snapshot` (full state, `rev`) then `patch` with `rev == base + 1`. On any
mismatch the client discards the patch and requests a new snapshot.

## Parsing hardening

- Messages over 64 KiB → `E_PAYLOAD_TOO_LARGE`.
- Duplicate JSON keys, `NaN`/`Infinity`, non-object top level, unknown fields,
  unknown ops → `E_SCHEMA`.
- `v` other than 1 → `E_UNSUPPORTED_VERSION` (command id echoed when readable).

## Leases and jobs

- `receiver.claim` takes the receiver's lease; `takeover: "preempt_with_notice"`
  succeeds over another holder only if the receiver's configured policy is also
  `preempt_with_notice`. Otherwise `E_BUSY`, and the holder is untouched.
- A preempted job shows `state: "preempted"`, `holds_lease: false` and a
  `detail` naming who took over. It does not silently re-take the receiver.
- Operator leases end when their session closes; job leases never depend on a
  session. Jobs are created server-side until the M4 scanner.
- Hardware commands require the lease (`E_NOT_LEASE_HOLDER`) and run strictly
  one at a time per receiver, in submission order.

## Idempotency

Retrying a command `id` within 10 minutes returns the original `ack` (and
`result`, if finished) without executing again, including from a new session
after a reconnect.

## Deferred within v1 (additive later)

- Hardware gain and antenna worker messages: defined with M1/M2 hardware.
- Client job/scan operations: with the M4 scanner.
- Listener audio controls: per-session, never global state.
- Channels: `channel.open` returns `E_MODE_UNAVAILABLE` until modes exist (M1).
