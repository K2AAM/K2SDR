# Worker DSP protocol additions (M1, additive within v1)

These are additions to `frame_v1` and `worker_ctl_v1`. The C headers in `protocol/` are
canonical; the Python mirrors in `k2websdr.protocol` are ABI-checked by
`control/tests/test_abi_m1.py`. The DSP behind them is described in
`docs/design/M1-dsp.md`. Nothing here changes M0 behaviour: IQ `SAMPLES` frames stay on by
default and the new outputs are off until asked for.

## Data plane (frame_v1)

New kinds carry a fixed extension right after the 80-byte header. `header_len` = 80 + the
extension; readers already skip header bytes they do not know.

| | SPECTRUM (kind 3) | AUDIO (kind 4) |
|---|---|---|
| format | `U8DB` (5), 1 byte per bin | `RI16`, mono |
| header_len | 96 | 104 |
| sample_count | FFT size (bins, FFT-shifted: bin 0 = −rate/2) | audio samples |
| sample_rate_hz | capture rate | 12000 |
| freq_origin_hz | capture centre | channel's tuned frequency (centre + offset) |
| stream_id | 0 | channel_id |
| first_sample_index | capture sample index just past the newest sample used | audio index within channel_gen |
| NEW_GEN | first frame of a capture generation | first frame of a channel_gen |
| DISCONTINUITY | samples used were not contiguous | a capture discontinuity fell in this frame's source |
| extension | db_floor_mdb, db_step_mdb, blocks averaged | channel_gen, mode, agc, S-meter level (centi-dBFS), passband |

- **Spectrum levels:** dBFS = (db_floor_mdb + q · db_step_mdb) / 1000. The worker sends
  −127.5 dBFS floor in 0.5 dB steps; 0 dBFS is a full-scale tone centred in a bin.
- **`seq`** counts every frame on the data connection, all kinds together.
- **Audio** is the channel's output after its AGC and before volume/mute. This is the decoder
  tap point.

## Control plane (worker_ctl)

| Message | Payload | Reply |
|---|---|---|
| `CHANNEL_OPEN` 0x0010 | `kctl_channel_v1` (40 B) | ACK, then `CHANNEL_APPLIED` |
| `CHANNEL_TUNE` 0x0011 | `kctl_channel_v1`: the full desired config | ACK, then `CHANNEL_APPLIED` |
| `CHANNEL_CLOSE` 0x0012 | `kctl_channel_close_v1` (8 B) | ACK, then `CHANNEL_APPLIED` (open = 0) |
| `CHANNEL_APPLIED` 0x0085 | `kctl_channel_applied_v1` (64 B): channel_gen, capture_gen, resolved config (defaults filled in), channel rate, open | — |

`kctl_channel_v1` fields:
- channel_id;
- mode (AM 1, USB 2, LSB 3, CW 4, NFM 5, WFM 6);
- AGC (Off 1, Fast 2, Medium 3, Slow 4);
- offset_hz (relative to the capture centre);
- passband edges (relative to the tuned frequency);
- CW BFO;
- manual gain (centi-dB, used with AGC off);
- flags `DEFAULT_PASSBAND` / `DEFAULT_BFO`. Only these flags select defaults: without
  `DEFAULT_PASSBAND` both edges are taken literally (every int32 is an edge, including
  INT32_MIN, which is simply out of range). CHANNEL_APPLIED and the AUDIO extension report the
  edges of the filter that is running.

All frequencies are integer Hz.

**Refusals at ACK.** In every case below the channel is left unchanged, and the worker keeps
running:

| Result | When |
|---|---|
| `E_BUSY` | the channel_id is already open, or 16 channels are already open |
| `E_NOT_FOUND` (7, new) | TUNE or CLOSE of a channel that isn't open |
| `E_OUT_OF_RANGE` | passband, span, rate or BFO is invalid (any int64 offset is checked without overflow; existing channels and their generations are unchanged) |
| `E_UNSUPPORTED` | unknown mode or AGC value |
| `E_BAD_MESSAGE` | unknown flags, or a reserved field is not zero |

Structural errors still end the worker, as in M0: wrong length, bad header, or a sequence that
doesn't increase.

**`channel_gen`** is a per-worker counter. A TUNE that changes only `offset_hz` is a
phase-continuous retune and keeps the `channel_gen`. Any other change rebuilds the channel and
bumps it. Capture re-acquisition (`SET_CAPTURE`) also rebuilds every channel and the spectrum,
and bumps their generations. In the control service, late channel replies go to
`on_late_channel_applied`, separate from capture replies.

**Not yet:** `SET_HW` (attenuator, LNA, HF AGC, calibration) arrives with the Airspy source
(M1 step 4), because replay cannot honour it.

## Worker arguments

| Argument | Default | Meaning |
|---|---|---|
| `--iq-frames on\|off` | on | send raw `SAMPLES` frames (processing runs either way) |
| `--spectrum-fft N` | 0 (off) | spectrum FFT size: a power of two, 64..65536 (others exit 64) |
| `--spectrum-fps N` | 15 | spectrum frames per second, 1..100 |
| `--start-after-ping` | off | replay: send no frames, and start no pacing clock, until the first PING is answered. Channels are opened first, so where audio starts is deterministic |
