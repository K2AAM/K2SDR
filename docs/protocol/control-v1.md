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

## Re-authorization at execution

Hardware commands queue per receiver. Everything checked at acceptance (session
still open, permission, lease holder, `if_capture_gen` precondition) is checked
again immediately before the command is sent to the worker. A command that no
longer passes never reaches the hardware and gets its single `result` as
`failed` with `E_UNAUTHENTICATED`, `E_FORBIDDEN`, `E_NOT_LEASE_HOLDER` or
`E_STALE_PRECONDITION`. A command already sent to the hardware completes and
is reconciled normally.

## Idempotency

Retrying a command `id` returns the original `ack` (and `result`, if finished)
without executing again, including from a new session after a reconnect.
Retries are matched before any other check, so a full server never turns a
retry into a rejection or a second execution.

## Capacity

Three limits bound memory and queueing (defaults in `k2websdr.core.service`):

| Limit | Default | What it bounds |
| --- | --- | --- |
| `MAX_CMD_RECORDS` | 10,000 | Idempotency records: in-flight commands plus finished outcomes |
| `MAX_IN_FLIGHT` | 1,000 (always below the record limit) | Accepted hardware commands not yet finished, all receivers |
| `RECEIVER_QUEUE_LIMIT` | 64 | Hardware commands queued or executing on one receiver |

- A hardware command (`receiver.set_capture`) is checked against the in-flight
  and receiver-queue limits **before** it is accepted: before any field is marked
  `pending` and before it enters the receiver queue. When a limit is reached it
  is rejected with `E_BUSY` and a detail naming the limit.
- Capacity rejections are not recorded. They cannot grow the cache, and a
  retry of the same `id` later is evaluated afresh (it may then be accepted).
- Accepted commands are never evicted while queued or executing, and each gets
  exactly one final `result`.
- Finished outcomes (results and other rejections) are kept for 10 minutes
  after they finish, but share the record limit: under sustained load the
  oldest finished outcomes are evicted first, so retention can be shorter than
  10 minutes. A retry of an `id` whose finished outcome was evicted is treated as
  a new command. Because the in-flight limit is always below the record limit,
  evicting finished outcomes alone always keeps the total within bound.
- Lease claim/release, subscribe, hello and session cleanup are not hardware
  commands and are never refused for capacity.

## Session expiry on WebSockets

An idle WebSocket closes (code 4401) when its login session expires or is
revoked, without waiting for a client message: it wakes at the expiry time and
re-checks the session at least every `ws_session_recheck_s` (default 30 s).
Closing ends the core session, which releases its operator leases; server-job
leases are unaffected.

## Capture fields

`center_hz` uses `FrequencyField` (requested/applied: integer Hz, 0–10 GHz) and
`sample_rate_hz` uses `SampleRateField` (integer Hz, 1–4 294 967 295). Every
state commit and every snapshot is validated against these bounds.

## Deferred within v1 (additive later)

- Hardware gain and antenna worker messages: defined with M1/M2 hardware.
- Client job/scan operations: with the M4 scanner.
- Listener audio controls: per-session, never global state.
- Channels: `channel.open` returns `E_MODE_UNAVAILABLE` until modes exist (M1).
