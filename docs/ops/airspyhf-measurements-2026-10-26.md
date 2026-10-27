# Airspy HF+ measurements — Window 1, 2026-10-26

Plan: `docs/design/M1-first-live-receiver.md` §2 (H1–H8). Run by Adam on the Optiplex
(Debian 13) as `k2dev`, 18:13–18:38 EDT (22:13–22:38 UTC), with
`tools/window1/run_window1.sh` from PR #14. That is the version **before** the review
fixes in #15. Where that matters, it is noted below.

Antenna and feedline: Adam's existing OpenWebRX antenna, unchanged. Gain settings were
the library defaults throughout; none of the gain controls were changed (`-1` in every
recording's description).

Raw results stay on the Optiplex in `~/K2SDR/window1-results/20261026-181353/`
(log, full recordings, about 0.8 GB). The three clips are committed in
`fixtures/real/airspyhf/`.

## Summary

| Item | Status | Result |
|---|---|---|
| H1 versions | **measured** | libairspyhf 1.6.8 (header and runtime), firmware `R5.0.1-CD`, part id 2, serial `3B52978026BA46DA` |
| H2 rates | **measured** | 912, 768, 650, 456, 384, 228, 192 kS/s. `is_low_if` is 1 at ≤ 650 kS/s and 0 at 768/912. Output block 2048 samples at every rate |
| H3 gain controls | **measured (1b)** | Attenuator about 5 dB per step (41 dB over 0–8). WWV SNR unchanged (48–51 dB): the antenna noise dominates. LNA +8 dB level, +1 dB SNR. AGC overrides the attenuator. **Settings persist in the device across opens** |
| H4 sustained streaming | **measured** | 0 dropped samples in 60 s at every rate, 10 min at 768k and 10 min at 384k (1b: 2.7 % CPU, longest gap 6.2 ms) |
| H5 retune | **measured** | No tuning transient detectable at 1 ms resolution. **Every retune silently loses about 1490 samples (7.8 ms at 192k)**; the library reports 0. The switch lands 1.3–12.5 ms after the call-time sample count |
| H6 frequency accuracy | **measured** | −0.089 ppm at 10 MHz, −0.076 ppm at 15 MHz (WWV). Stored calibration 0 ppb. CHU too weak |
| H7 unplug | **measured (1b)** | `is_streaming` goes to 0 within about 1 s. **`airspyhf_stop`/`close` then hang**: Ctrl+C did not end the process; SIGQUIT did. Replug and reopen work (8 ms) |
| H8 recordings | **measured** | 7 recordings, 0 drops. 3 clips committed. FT8 clip decodes 22 messages |
| Busy device | **measured** | With OpenWebRX holding it, `open_sn` returns −1 in about 1 ms. Adam heard no audio glitch |

## Details

### Busy device (with OpenWebRX running)

`airspyhf_list_devices` still lists the device. `airspyhf_open_sn` fails with −1 after
about 1.1 ms. Adam was listening to OpenWebRX at the time and reported no glitch. So the
live receiver can rely on a failed open, together with our own `flock`, as its "busy"
signal, without disturbing the current owner.

### H2 and H4: rates, drops, CPU

60 s per rate at 10.000 MHz, plus 10 min at 768 kS/s. `dropped_samples` was 0 for every
callback in every run, and the writer queue never overflowed.

The #14 probe treated drop reports as cumulative (fixed in #15). That bug could only
under-count drops reported in consecutive callbacks. Any drop that follows a clean
callback was counted in full, so a total of 0 still means none were reported.

| rate (S/s) | low IF | measured rate | max callback gap | CPU, one core |
|---|---|---|---|---|
| 912000 | 0 | 910957 | 5.2 ms | 18.0 % |
| 768000 | 0 | 766954 | 5.4 ms | 15.6 % |
| 650000 | 1 | 649290 | 3.6 ms | 4.3 % |
| 456000 | 1 | 455486 | 4.9 ms | 3.2 % |
| 384000 | 1 | 383490 | 5.8 ms | 2.7 % |
| 228000 | 1 | 227728 | 9.5 ms | 1.7 % |
| 192000 | 1 | 191752 | 11.0 ms | 1.5 % |
| 768000 (10 min) | 0 | 767895 | 6.4 ms | 15.6 % |

- The measured rate is samples ÷ wall time. The roughly 0.1 % shortfall is the start-up
  delay: the first sample arrives about 85 ms after `airspyhf_start` (consistent across
  the 192 kS/s recordings). Per-second ticks after the first second match nominal to
  about ±0.3 %.
- The longest callback gap is about one block (2048 samples), as expected.
- **Low-IF rates (≤ 650 kS/s) cost about 4–6× less CPU** than 768/912 with the library's
  default DSP. This is an input for choosing the default rate.

The two rates the device reports that the design note's mock did not have (650k and
228k) are now in the mock.

### H5: retune

Measured from recording `retune` (192 kS/s; 10.005 → 15.005 → 10.005 → 15.005 →
10.005 MHz at 3 s intervals; WWV carriers at −5 kHz). The recording is not committed
(23 MB).

- `airspyhf_set_freq` returns in 0.28–0.42 ms.
- **Where the switch appears:** a sharp step in total power, 9.0, 12.5, 1.25 and 1.5 ms
  after the sample count taken at the call. That is either about 1.3 ms or about one
  block (2048 samples ≈ 10.7 ms) more, depending on whether a block was already in
  flight. **The call-time sample index is not the boundary.** The live receiver must
  treat at least one block, better two, after a retune as unknown. Generation tagging
  already isolates them; the source should drop them.
- **Settling:** after the step, no 1 ms window (in runs of ≥ 2) falls outside the range
  the band itself shows in steady state, on any of the four retunes. The WWV carrier
  phase is coherent straight after the step (clearest on retune 3). So the tuner
  relocks within the 1–2 ms resolution of this measurement.
- The analysis that ran during the window (`analyze.py settle`) reported 1073 ms, null,
  113 ms, null. That was **wrong**: it compared the carrier against all the power in a
  192 kHz band, and at 15 MHz that includes shortwave broadcasters. It is replaced by
  `analyze.py retune` (step location, transient test, carrier presence), checked against
  this recording and against the mock's simulated 40 ms transient.
- **Open: lost samples.** The per-second sample count dips by about 4000 samples
  (≈ 20 ms) in the second containing three of the four retunes, and the longest callback
  gap rises to about 18 ms. The library reported no drops. Total counts against wall
  time suggest only about 5–6 ms lost per retune. The two estimates disagree and both
  are coarse. **Measurement proposed (Window 1b):** log a host timestamp and the
  cumulative sample count for every callback around each retune, then fit
  samples-against-time before and after. A sample loss shows up as an offset in the fit.
  The intercept should be good to well under 1 ms.
- WWV second ticks were tried as a time reference on the 10 MHz clip, but they are too
  weak tonight (peak only 2–4× the median) and WWVH (1200 Hz ticks) shares the
  frequency. Rejected as a method for this data.

### H6: frequency accuracy

Long-window FFT (up to 4 s, from the second half of each recording; usable only if the
peak is ≥ 20 dB above the noise):

| carrier | measured | error | peak SNR |
|---|---|---|---|
| WWV 10 MHz | 9 999 999.11 Hz | −0.89 Hz, **−0.089 ppm** | 48.2 dB |
| WWV 15 MHz | 14 999 998.87 Hz | −1.13 Hz, **−0.076 ppm** | 53.8 dB |
| CHU 7.85 MHz | — | not usable | 12.7 dB |

The two WWV results agree in ppm (an oscillator error scales with frequency). The
receiver reads about 0.08 ppm low, so its clock runs about 0.08 ppm fast. The stored
calibration is 0. At night, ionospheric Doppler on skywave paths can be a fraction of a
hertz, which is the same size as this error. **This is not a calibration value yet.**
Average several sessions, at different times of day, before writing a calibration.

### H8: recordings and fixtures

| recording | centre | rate | length | drops |
|---|---|---|---|---|
| wwv10 | 10.005 MHz | 192k | 45 s | 0 |
| wwv15 | 15.005 MHz | 192k | 45 s | 0 |
| chu785 | 7.855 MHz | 192k | 30 s | 0 |
| 40m | 7.120 MHz | 192k | 60 s | 0 |
| mw | 1.000 MHz | 912k | 30 s | 0 |
| fm | 100.1 MHz | 912k | 20 s | 0 |
| retune | see H5 | 192k | 15 s | 0 |

Committed clips (`fixtures/real/airspyhf/`, ci16_le, 48 kS/s, `core:sha512`, each
checked by `control/tests/test_real_fixtures.py`):

- `airspyhf_wwv10_48k`: 10.001 MHz centre, 5 s. WWV carrier at −1000 Hz; WWVH is also
  visible.
- `airspyhf_ft8_40m_48k`: 7.0755 MHz centre, 15 s, **one complete FT8 slot**
  (22:33:30 UTC). Re-cut after the window because the original 16 s clip straddled two
  slots and decoded nothing. USB-demodulated at dial 7.074 MHz and run through ft8_lib
  `decode_ft8` (kgoba/ft8_lib @ 9fec6ca, MIT; offline check only, not a dependency), it
  decodes **22 messages at +7.5 to +17.5 dB**. They are listed in
  `airspyhf_ft8_40m_48k.decodes.txt`. Decode time offsets of about +1.2 s fit a clip
  starting at about :29.5, so recording timestamps are good to a few tenths of a second.
- `airspyhf_ssb_40m_48k`: 7.190 MHz centre, 10 s, voice traces on 40 m SSB.

`core:datetime` in these three metadata files was corrected from the recording's start
time to the clip's start time; `core:sha512` covers the data only. `clip.py` now writes
the clip start time itself.

The callsigns in the FT8 decodes are public amateur transmissions. They are **not**
location evidence and must never produce map markers.

### H7: unplug

At `UNPLUG NOW` the stream kept running at full rate for the whole 60 s, so the cable
was not pulled. The `#14` script printed the exit code, but nothing checked it; `#15`
now treats "no device loss" as a failure. Still open; it goes into Window 1b.

### OpenWebRX after the window

The service was active again with no restarts, `soapy_connector` was running, and HTTP
returned 200. The only differences from the before-snapshot are the process IDs and
temporary ports. Adam confirmed the audio worked.

## Consequences for M1

1. Pick the capture rate from the low-IF set (≤ 650 kS/s) unless a band plan needs
   more. CPU is about 4× lower. 384 kS/s is confirmed clean for 10 min (Window 1b).
2. Every retune starts a new generation. Samples are silently lost (Window 1b: about
   1490 at 192k), and the switch lands up to about one block after the call-time index.
   Discard at least 2 blocks after the retune and never infer timing across it.
3. No calibration correction yet (H6). Show "uncalibrated, measured ≈ −0.08 ppm" in the
   UI when calibration lands.
4. The busy-device error path is a fast, harmless open failure.
5. On device loss, exit without calling `airspyhf_stop`/`close` (they hang); the
   supervisor restarts the worker.
6. Set every control explicitly on open; device state persists between processes.

## Window 1b — 2026-10-26, 23:42–23:55 UTC

Run by hand in Adam's server room, with OpenWebRX stopped. Commands are in the session
notes. The probe came from `main` after #15, and the retune timing from branch
`m1/window1b-timing` (`--timing-log`, `tools/window1/timing.py`). Raw files are in
`~/K2SDR/window1-results/1b/` on the Optiplex.

### H7: unplug

At 768 kS/s Adam pulled the cable at about t = 11 s. The next one-second tick (t = 12.1 s)
showed `streaming: 0`, and the probe reported `streaming_stopped` and left its loop. It
then **hung** in `airspyhf_stop()`/`airspyhf_close()`. Ctrl+C had no effect (the signal
handler only sets a flag the loop has already stopped checking), and SIGQUIT (Ctrl+\)
ended it with exit 131. After replugging, `busy-check` opened the device in 8.2 ms.

**Consequence:** the acquisition worker must never block on `airspyhf_stop`/`close` after
device loss. On loss it reports the error over its control socket and exits
(`_exit`) without calling them. The supervisor (control service) restarts it, and the
OS releases USB when the process ends. Any stop/close on the normal path needs a
watchdog. Detection latency was ≤ 1 s here, limited by the probe's 1 s polling; the
worker can poll `is_streaming` faster and also watch the callback cadence.

### H5: samples lost on retune

7 retunes, alternating 10.005 / 15.005 MHz at 3 s intervals, 192 kS/s. For each one,
samples-delivered-against-host-time was fitted on the roughly 85 callbacks either side
(guard ±0.1 s):

| retune | lost samples | lost ms | ±1σ | library drops | longest callback gap |
|---|---|---|---|---|---|
| 1 | 1480 | 7.71 | 4 | 0 | 18.5 ms |
| 2 | 1474 | 7.68 | 3 | 0 | 18.4 ms |
| 3 | 1481 | 7.72 | 4 | 0 | 18.5 ms |
| 4 | 1484 | 7.73 | 4 | 0 | 18.5 ms |
| 5 | 1514 | 7.89 | 5 | 0 | 18.5 ms |
| 6 | 1510 | 7.86 | 4 | 0 | 18.3 ms |
| 7 | 1493 | 7.77 | 3 | 0 | 18.5 ms |

The fitted rates before and after agree with each other and with 192000 to within
±40 Hz, so the offset is not a clock effect.

**It is lost, not delayed.** Over the whole run the shortfall is 192000 × 24.01 −
4 583 424 = 26 496 samples. That equals the start-up delay (about 85 ms, about 16 300
samples, the same as in Window 1) plus 7 × about 1490. A pure added latency could not
accumulate like that.

Measured at 192 kS/s only. Whether the loss is fixed in samples or in time across
rates is not known; measure it at the chosen default rate before relying on a number.

**Consequence:** a retune is a discontinuity of unknown exact length, and the driver
does not report it. The worker starts a new capture generation on every retune (the
frame protocol already restarts `first_sample_index` per generation). It never carries
sample-count timing across a retune; it re-anchors `host_time_ns` per generation. It
discards the first ≥ 2 blocks after the step (H5 step location above). The earlier
coarse estimates (about 4000 from per-second ticks, 5–6 ms from totals) are superseded.

### H3: gain controls

10.005 MHz, 192 kS/s, 5 s each, in this order. Levels are mean power in dBFS; SNR is
the WWV carrier peak-to-median within ±2 kHz:

| run | set by the probe | rms dBFS | peak (full scale = 1) | WWV SNR |
|---|---|---|---|---|
| att0 | agc 0, att 0 | −28.6 | 0.075 | 51.3 dB |
| att2 | agc 0, att 2 | −35.3 | 0.042 | 47.8 dB |
| att4 | agc 0, att 4 | −46.3 | 0.009 | 51.3 dB |
| att6 | agc 0, att 6 | −56.9 | 0.003 | 50.0 dB |
| att8 | agc 0, att 8 | −70.0 | 0.001 | 48.5 dB |
| lna0 | agc 0, lna 0 | −80.9 | 0.000 | 48.4 dB |
| lna1 | agc 0, lna 1 | −73.0 | 0.000 | 49.6 dB |
| agc0 | agc 0 | −78.2 | 0.000 | 42.0 dB |
| agc1 | agc 1 | −29.1 | 0.077 | 48.4 dB |

- **The attenuator works, at about 5 dB per step** (41 dB over 0–8; fading between the
  5 s runs blurs individual steps). SNR does not change: on this antenna at 10 MHz the
  received band noise is well above the receiver's own noise even at index 8. So
  attenuation is free here and is the tool against overload.
- **Settings persist in the device between opens.** The lna and agc runs did not set
  `hf_att`, yet their levels (−73 to −81 dBFS) sit with att8 (−70), the value the
  previous process had set, not with att0. Inferred from levels, consistent across four
  runs. It also means Window 1 ran with **whatever OpenWebRX had last set**, so the
  "library default (−1)" in those recordings is unknown, not a default.
- **LNA:** +7.9 dB of level, +1.2 dB of SNR (within run-to-run fading). No meaningful
  SNR gain at 10 MHz on this antenna.
- **AGC on** overrides the attenuator: with att still 8 from earlier, agc1 came back
  to −29 dBFS (≈ att0).
- Headroom: even at att0/AGC the peaks are 0.077 of full scale at 10 MHz in the
  evening. Overload is more likely on MW and the broadcast bands. That is not measured
  here.

**Consequence:** on every open, the worker sets each control explicitly (sample rate,
frequency, AGC, AGC threshold, attenuator, LNA, lib DSP) and reads none of them as
"default". Proposed starting defaults, all configuration and reversible, for Adam to
approve: AGC on, AGC threshold low, LNA on, attenuator 0 (it only acts with AGC off).
Revisit them on MW, where overload is more likely.

### H4: 10 minutes at 384 kS/s

7.100 MHz, 600 s: 230 371 328 samples, **0 dropped**, longest callback gap 6.2 ms,
**2.7 % of one core**.

## Status after Window 1b

H1–H8 are all measured, with the noted limits: H6 needs more sessions before it becomes
a calibration value, and the H5 loss is measured at 192 kS/s only. That is enough to
start the M1 acquisition source on real hardware assumptions: low-IF default rate, a
generation per retune with a discard, bounded exit on device loss, and explicit control
settings on every open.
