# Browser stream v1 — spectrum rows and a session's own audio

M1 step 3b-2. Implementation: `control/src/k2websdr/protocol/stream.py` (codec),
`core/service.py` (`Session` queue, `ControlCore._on_frame`), `web/app.py` (`/ws` pump).
Design: `docs/design/M1-first-live-receiver.md` §3.3–3.4.

## Transport

Stream messages are **binary** WebSocket messages on the control socket `/ws`, next to the
control protocol's JSON (text) messages. One socket per browser session, so everything a
session receives is authorised by that session (cookie, Origin, re-checked expiry and
revocation, as for control messages). There is no separate stream endpoint.

- **Spectrum:** `stream.spectrum {receiver, enabled}` (permission `listen`, immediate result)
  turns SPECTRUM messages for that receiver on or off for this session. Off by default.
- **Audio:** a session receives AUDIO for the channels it owns (`channel.open`), and only
  those. Closing a channel, or the session, stops its audio and discards queued frames.

Clients never send binary messages.

## Message layout

Little-endian. A 64-byte fixed header, the id (UTF-8, 1–64 bytes, zero-padded to a multiple of
4), then the payload at `header_len`.

| off | size | field |
|---:|---:|---|
| 0 | u32 | magic `0x4253324B` (bytes `K2SB`) |
| 4 | u8 | version = 1 |
| 5 | u8 | kind: 1 SPECTRUM, 2 AUDIO |
| 6 | u16 | header_len = 64 + padded id length |
| 8 | u32 | flags: `NEW_GEN` 1, `DISCONTINUITY` 2, `SERVER_DROP` 4 |
| 12 | u32 | count: bins (SPECTRUM) or int16 samples (AUDIO) |
| 16 | u64 | gen: `capture_gen` (SPECTRUM) or `channel_gen` (AUDIO) |
| 24 | u64 | sample index: SPECTRUM end sample (exclusive) in the capture; AUDIO first sample |
| 32 | i64 | `frequency_hz`: SPECTRUM capture centre; AUDIO tuned (dial) frequency. Integer Hz |
| 40 | u32 | `sample_rate_hz`: SPECTRUM capture rate; AUDIO 12000 |
| 44 | 16 B | SPECTRUM: i32 `db_floor_mdb`, u32 `db_step_mdb`, u32 `blocks`, u32 reserved 0 |
|    |      | AUDIO: i32 `level_cdb`, i32 `pass_lo_hz`, i32 `pass_hi_hz`, u16 mode, u16 agc |
| 60 | u8 | id_len: SPECTRUM receiver id; AUDIO channel id (`ch_…`) |
| 61 | 3 B | reserved 0 |

- **SPECTRUM payload:** `count` uint8 bins, FFT-shifted: bin k is `(k − count/2) · rate / count`
  Hz from the centre. Value q is `db_floor + q · db_step` dBFS (fields in milli-dB; today
  −127.5 dBFS and 0.5 dB). `blocks` FFTs were power-averaged. The FFT size and row rate are
  server settings (`spectrum_fft`, default 2048, a power of two; `spectrum_fps`, default 15).
- **AUDIO payload:** `count` int16 mono samples at 12000 S/s, before volume, mute and listening
  squelch (those are browser-side). Mode and AGC use the worker_ctl enums
  (`docs/protocol/worker-dsp-v1.md`); `level_cdb` is the channel level before AGC in centi-dBFS;
  the passband edges are those of the filter that is running.
- **Flags:** `NEW_GEN` starts a generation (sample index restarts at 0). `DISCONTINUITY` means
  samples were lost or are missing upstream (recorded gap, worker overflow). `SERVER_DROP`
  marks the first message of a stream after the server discarded messages of that stream for
  this client (see below).

## Generations

The server never sends a stale frame (outline §13, acceptance B6):

- SPECTRUM is sent only if its `capture_gen` is the receiver's current one.
- AUDIO is sent only if its channel exists, is open and owned by this session, its
  `channel_gen` is the channel's current one, and its `capture_gen` is current.

The server learns a new `channel_gen` from the worker's CHANNEL_APPLIED, which travels on a
different socket from the frames, so the first frames of a new generation can arrive before
it and are dropped too: a short loss at a mode change, never stale audio. Clients should still
drop anything whose `gen` is older than the one in the latest `result`/state.

## Backpressure

A slow client must not delay control messages or grow server memory:

- One sender per socket: queued control messages (JSON) always go first, then audio, then
  spectrum.
- Spectrum keeps only the newest pending row per receiver; replaced rows count as dropped.
- Pending audio per session is bounded (`STREAM_AUDIO_QUEUE_BYTES`, 96 000 bytes, about 1 s for
  4 channels); when full, the oldest audio frames are dropped.
- Either way the next message of that stream carries `SERVER_DROP`, once.

The session keeps counters (`stream_sent`, `stream_dropped`) for measurements (B4, B7).

## Not yet

- Measurements on a real browser and iPhone (underruns, latency, bandwidth): with the UI (step 5).
- Passband/AGC/BFO controls on `channel.*`, and a compressed audio codec: later, by measurement.
