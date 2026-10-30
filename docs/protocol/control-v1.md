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

## Channels (M1)

`channel.open` / `channel.tune` / `channel.close` need the `listen` permission and **no lease**:
a channel is a listener's own demodulator inside the receiver's current capture span, not
global device state. Frequencies are absolute dial `frequency_hz`; the server converts to an
offset from the applied capture centre for the worker.

- **Availability** (outline: adapter + dependencies + hardware bandwidth): the mode must be in
  the ledger's `available` list (`ledger/integrations.v1.json`) *and* fit this receiver
  (complex IQ; capture rate ≥ the mode's channel rate: 12 kS/s AM/USB/LSB/CW, 24 kS/s NFM,
  192 kS/s WFM). Otherwise `E_MODE_UNAVAILABLE` with the reason.
- **Span**: the mode's default passband around `frequency_hz` must lie inside the capture span,
  else `E_OUT_OF_RANGE` with the frequencies that would fit.
- **Lifecycle**: ack, then one result after the worker applied it. `result.channel` and
  `result.channel_gen` identify it; the state gains `channels.<id>` only once applied.
- **Ownership**: only the session that opened a channel may tune or close it (`E_FORBIDDEN`).
  Closing the session closes its channels.
- **Generations**: tuning only the frequency keeps `channel_gen`; a mode change bumps it.
  `channel.tune` accepts `if_channel_gen` (`E_STALE_PRECONDITION`).
- **Capture changes**: after a capture readback the server re-tunes every channel to keep its
  absolute `frequency_hz` (new `channel_gen`); a channel that no longer fits is closed and
  removed from the state. After a worker restart, channels are opened again.
- **Limits**: 4 channels per session and 16 per receiver (`E_BUSY`); at most 8 channel commands
  of one session waiting for the worker (`E_BUSY`, not recorded, like other capacity limits).
- **Listener controls** (M1 UI; `channel.open` and `channel.tune`):
  - `agc`: "off" | "fast" | "medium" | "slow" (default medium). With "off", `manual_gain_db`
    (0..100) is the fixed demodulator gain.
  - `passband_hz`: `[low, high]`, integer Hz relative to the dial frequency
    (|edge| ≤ 100 000, low < high). Absent on open means the mode's default. On tune,
    `passband_default: true` returns to it. A mode change without a new `passband_hz` also
    returns to the new mode's default, because a custom filter belongs to its mode.
  - **Checks:** the span check uses the real passband. The worker also refuses edges beyond
    the mode's channel bandwidth (`E_OUT_OF_RANGE` as the result).
  - **State:** `channels.<id>` reports `agc`, `manual_gain_db`, `passband_default` and
    `passband_hz`. The last is the worker's readback of the filter actually running.
  - **Generations:** a change to any of these rebuilds the channel (new `channel_gen`). CW
    pitch (BFO) stays at 700 Hz.
- **Audio** of a channel goes to its owner only, as binary stream v1 messages on the same socket
  (`docs/protocol/stream-v1.md`).

## Hardware gain (M1, Airspy HF+)

`receiver.set_hw_gain {receiver, hf_agc?, hf_agc_threshold? ("low"|"high"), hf_lna?, hf_att? (0..8)}`

- **Rules:** the gain is global device state, like the capture, so it needs the lease and
  `hw.retune` (the outline has no separate gain permission). It runs in the receiver's
  serialized queue, in order with retunes.
- **Partial changes compose:** each command starts from the newest wish (a pending request,
  else the applied value), so two queued changes do not undo each other. The worker always
  sets all four controls.
- **State:** `capture.hw_gain` (`FieldState` of `{hf_agc, hf_agc_threshold, hf_lna, hf_att}`)
  follows the same pending → applied/failed rules as the capture fields. `verified` stays
  `"none"`: libairspyhf has no getters.
- **Generations:** a gain change does not start a new generation.
- **Refusals:** replay receivers answer `E_UNSUPPORTED`. A driver error gives a failed result
  (`E_DEVICE_FAILED`), and the worker restarts.

## Live receivers: loss and restart (M1)

`driver = "airspyhf"` receivers restart their worker without limit. The backoff is 0.5 s
doubling to 30 s, and resets after a worker ran for 60 s.

- **While waiting,** `acquisition` shows the reason and the retry: `failed` "device not found
  or lost; retrying in N s", or `external_busy` "device in use by another process; …" when
  another program (OpenWebRX+) holds it.
- **A restarted worker** opens with the last applied centre, rate and gain (not the configured
  initial ones), so the receiver comes back where it was. Channels are reopened (new
  `channel_gen`).
- **Replay receivers** keep the M0 policy: at most 3 restarts.

## Recordings (M1, design §3.6)

`recording.start {receiver, frequency_hz, mode, agc?, passband_hz?, manual_gain_db?}` and
`recording.stop {recording}` (permission `record`).

- **What is recorded:** the clean demodulated audio (12 kS/s mono int16, exactly as the worker
  made it, before anything a browser does).
- **Its own channel:** a recording runs on a **server-owned channel**
  (`channels.<id>.owner.job`), so it keeps going when the browser is suspended or closed. It
  runs until stopped by its owner (any session of the same user) or an admin. It also ends when
  storage runs out, or when a receiver retune leaves its frequency outside the capture
  (`interrupted`). At most 4 per user.
- A `receiver.set_capture` that would leave an active recording (anyone's) outside the new
  capture is rejected with `E_RECORDING_ACTIVE`. The detail names the recordings. The same
  command with `end_recordings: true` retunes and ends them (`interrupted`); the UI asks first
  ("Still recording … End recording and retune / Cancel"). The check runs again when the retune
  reaches the front of the hardware queue. A recording that is not in the confirmed set by then
  fails the retune with `E_RECORDING_ACTIVE`; a recording that is still opening counts too.
  `recording.start` is refused (`E_BUSY`) while a queued or running retune would not cover it.
  A recording can still end `interrupted` without confirmation only when the device lands
  somewhere other than requested.
- **Result:** `result.recording` and `result.channel`. While active, `recordings.<id>` shows
  frequency, mode, file, seconds, bytes and gaps (discontinuities inside the file), updated
  about once a second.
- **Filename:** `<UTC>_<frequency_hz>_<mode>.wav`, e.g. `20261027T183000Z_7074000_usb.wav`.
  The same integer Hz is in the state and the catalog (B5). The WAV header is rewritten every
  5 s, so a file is playable even after a crash.
- **Storage:** `storage {used_bytes, cap_bytes, free_bytes, full, detail}`.
  - Config sets the cap: `recordings_cap_bytes` (default 2 GiB) and
    `recordings_min_free_bytes` (default 512 MiB).
  - When either is reached, new recordings are refused (`E_STORAGE_FULL`) and active ones end
    with status `storage_full`.
  - Nothing is deleted automatically.
- **Catalog:** the SQLite table `recordings`; files are in `<state_dir>/recordings/`. A
  recording left `recording` by a crashed process is marked `interrupted` at startup.
- **HTTP:** needs `record`; users see their own recordings, admins see all.
  - `GET /api/recordings` lists them.
  - `GET /api/recordings/<id>/file` downloads the WAV.
  - `DELETE /api/recordings/<id>` needs the CSRF header; an active recording is refused with
    409.

## Spectrum stream (M1)

`stream.spectrum {receiver, enabled}` needs `listen` and gives an immediate result. While
enabled, the socket receives that receiver's spectrum rows as binary stream v1 messages
(`docs/protocol/stream-v1.md`): only the current `capture_gen`, newest row first under
backpressure. Off by default and per session.

## Deferred within v1 (additive later)

- Hardware gain and antenna worker messages: defined with M1/M2 hardware.
- Client job/scan operations: with the M4 scanner.
- Listener audio controls: per-session, never global state.
