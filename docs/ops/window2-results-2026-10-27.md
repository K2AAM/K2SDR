# Window 2 results — 2026-10-27 (first live run of the M1 worker)

Run by Adam on the Optiplex over SSH, with OpenWebRX stopped for the window. The setup:
- **Code:** `tools/window2/live_check.py` from branch `m1/window2`, driving
  `build/worker/ksdr-worker --source airspyhf` on the Airspy HF+ Discovery, serial `3B52978026BA46DA`.
- **Antenna:** W6LVP loop.
- **Runs:** a full run at 16:32–16:45 UTC and an unplug-only rerun at 16:53 UTC.
- **Bundles:** `/tmp/k2-window2-20261027-123233.tar.gz` and `…-125356.tar.gz`.

The service (`driver = "airspyhf"`), the browser stream and the UI were **not** part of this
window. They were tested here with the mock only.

## Before the run: device held by OpenWebRX

The first attempt ran with OpenWebRX still running. The worker failed fast with STATUS
`E_BUSY`, exit 75, and touched nothing. That is the path the service shows as
`external_busy`.

## Results

| Step | Result |
|---|---|
| Open | All controls set on open (worker-live-v1). Streaming at 384 kS/s, spectrum 2048 bins at 10 rows/s |
| 10-min run, 384 kS/s, spectrum + AM + USB channels | 5999 of 6000 expected rows. **0 discontinuities** in spectrum and both audio channels (56 249 frames each, contiguous). Worker RSS 14.5 MB |
| Worker CPU | **19.4 % of one core.** Higher than expected (the bare probe used 2.7 % at 384k in Window 1b; the DSP benchmark predicts a few % more). To be profiled (open item) |
| `SET_HW` gain sweep (WWV 10 MHz carrier / spectrum median, dBFS) | AGC on: −66.5 / −73.8. Att 0: −66.2 / −73.9. **Att 4: −92.1 / −98.2. Att 8: −114.5 / −121.9.** LNA off (att 0): −73.0 / −79.5. AGC on again: −68.1 / −73.6 |
| Retune 10 → 15 → 7.1 → 29.6 → 10 MHz | APPLIED in 0.6–2.0 ms. First new-generation spectrum row 123–128 ms later. **0 old-generation rows after APPLIED** |
| Rate change 384k → 192k → 384k (stop/set/start) | 4.3 / 5.8 ms. AM audio afterwards contiguous |
| **USB, 7.074 MHz** | **FT8 decodes with ft8_lib `decode_ft8` (@ 9fec6ca, offline):** slot 1 `K1CX N2BRC RR73`. Slot 2: `N2BRC AC2GI FN21`, `N2BRC K1CX 73`, `W4MAA K1EI R-17`, `W9ZPL KC3IKC +08` |
| **AM, WWV 15 MHz** | WWV content present: **600 Hz tone +38 dB, 100 Hz time code +28 dB** over the audio median |
| AM, WWV 10 MHz | **Not WWV-like:** 120 Hz +31 dB with 60/240/360/480/720 Hz, i.e. mains harmonics. Same after the rate change. Open item |
| LSB 7.200, CW 7.030 MHz | Chains ran, contiguous. Only weak or no signals, so no quality claim |
| NFM / WFM at 29.6 MHz | Chains ran on live samples, contiguous. No FM signal expected on HF, so no quality claim |
| **Unplug** (virtual: `authorized = 0` on the USB port) | STATUS FAILED/`E_DEVICE`, **exit 69 2.3 s after the disconnect, no hang, stop/close not called** |
| Replug (`authorized = 1`) | Reopened on the first try, in 0.02 s. 29 spectrum rows in the next 3 s |
| Normal stop | Exit 0 in 0.01 s (the stop/close watchdog was not needed) |

**Script bug in the first run.** A key pressed earlier answered the replug prompt instantly,
and the 10 retries ran out while the device was still de-authorized. The script now flushes
input before prompts and retries for 60 s (`--only-unplug` reran just that part).

**Finding: a de-authorized device is listed but cannot be opened.** The worker then reports
`E_BUSY`, the same as "held by another program". The message now says "in use by another
program, or not authorized".

## Conclusions

- **Attenuator ≈ 6.1 dB per step** (−25.9 dB over steps 0→4 and −22.4 dB over 4→8 on the
  carrier). This matches libairspyhf's documented 6 dB steps. Window 1b's "about 5 dB" was
  blurred by fading between separate 5 s runs; here the steps were 4 s apart on one stream.
  LNA ≈ +6 dB. AGC on ≈ attenuator 0. SET_HW verifiably acts on the hardware.
- **Live audio verified for USB (FT8 decodes) and AM (WWV 15 MHz tones).** LSB, CW, NFM and
  WFM run on live samples, but their demodulation quality on air is not shown yet.
- **The retune discard and generations behave as designed on hardware.**
- **Device loss and recovery work on hardware at the worker level,** for the virtual
  disconnect. Window 1b covered the physical pull for the probe. The service-level
  automatic restart was tested with the mock only.

## Open items

1. **WWV 10 MHz hum: points to the RF side.** Adam listened on OpenWebRX right after the
   window (same antenna, 12:57 local): "10 MHz WWV is very faint, 15 MHz is slightly better".
   - **Our spectrum agrees:** the 10 MHz carrier was only about 7 dB above the noise median
     (−66.5 vs −73.8 dBFS per 188 Hz bin).
   - **Interpretation:** the AM channel's AGC brought a very weak carrier up to listening
     level, and with it the band noise, whose 120 Hz-and-harmonics structure is typical of
     power-line or switching-supply noise.
   - **Not a defect claim either way:** re-check at a time when WWV 10 MHz is strong (Window 1
     measured 48–51 dB SNR in the evening). If the hum persists then, look for a local source.
2. **Worker CPU 19 %.** Measure the split (driver thread, ring/eventfd, DSP) before the service
   runs it around the clock.
3. **The physical unplug with the worker,** in a later window (this run was the virtual
   disconnect).
