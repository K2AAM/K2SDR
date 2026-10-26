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
| H3 gain controls | **not measured** | Library defaults only. Still open |
| H4 sustained streaming | **measured (partly)** | 0 dropped samples in 60 s at every rate and in 10 min at 768 kS/s. The plan asked for 10 min per *candidate* rate |
| H5 retune | **measured (partly)** | No tuning transient detectable at 1 ms resolution. The switch lands 1.3–12.5 ms after the call-time sample count (one USB block of uncertainty). Whether samples are lost is open |
| H6 frequency accuracy | **measured** | −0.089 ppm at 10 MHz, −0.076 ppm at 15 MHz (WWV). Stored calibration 0 ppb. CHU too weak |
| H7 unplug | **not measured** | The cable was not pulled during the test (samples kept arriving at full rate). Still open |
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
   more. CPU is about 4× lower. Default candidate: 384 or 456 kS/s. Confirm with a
   10 min run at that rate (Window 1b or 2).
2. After every retune, discard a fixed number of samples, at least 2 blocks (4096
   samples). Do not rely on the call-time index. Size this finally after the Window 1b
   timing measurement.
3. No calibration correction yet (H6). Show "uncalibrated, measured ≈ −0.08 ppm" in the
   UI when calibration lands.
4. The busy-device error path is a fast, harmless open failure.

## Window 1b (proposed, about 10 minutes, OpenWebRX stopped)

- H7: the unplug test, done properly.
- H5: retune with per-callback timing (needs a small probe addition: `--timing-log`).
- H4: 10 min at the chosen low-IF default rate.
- H3: a short sweep of the HF attenuator 0–8 and the LNA on/off against WWV, to set the
  gain defaults.
