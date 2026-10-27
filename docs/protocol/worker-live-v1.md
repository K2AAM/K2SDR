# Worker live source v1 — Airspy HF+ (`--source airspyhf`)

M1 step 4a. Code: `worker/src/airspyhf_source.{hpp,cpp}`, `worker/src/live_source.hpp`, the live
paths in `worker/src/worker.cpp` and `main.cpp`. Python: `worker_ctl.SetHw/HwApplied`,
`WorkerProcess.set_hw`, `replay.worker_argv.airspyhf_argv`. Tests:
`control/tests/test_worker_airspyhf.py` (mock libairspyhf), `test_abi_set_hw.py`.
Hardware facts used here: `docs/ops/airspyhf-measurements-2026-10-26.md`.

**Not validated on hardware yet.** Everything below is tested against
`worker/probe/mock_airspyhf.c`, which models what Windows 1 and 1b measured. Hardware Window 2
runs these paths against the real device.

## Starting

```
ksdr-worker --device-id ID --lock-dir DIR --receiver-idx N --stream-id N --ctl-fd FD --data-fd FD
            --source airspyhf --serial 16HEX --rate-hz N --center-hz N
            [--hf-agc on|off] [--hf-agc-threshold low|high] [--hf-lna on|off] [--hf-att 0..8]
            [--capture-gen-start N] [--frame-samples N] [--iq-frames on|off] [--spectrum-fft N] [--spectrum-fps N]
```

- **Lock, then device.** The per-device `flock` is taken before libairspyhf is called. The
  device is opened by serial only (`airspyhf_open_sn`).
- **Every control, every time.** Device settings persist between processes (Window 1b, H3).
  So `open` sets them in this order: sample rate, frequency, lib DSP (on), HF AGC, AGC
  threshold, LNA, attenuator. The defaults are Adam's approved starting values: AGC on,
  threshold low, LNA on, attenuator 0. The control service passes all four explicitly.
- **Failures:**

  | case | STATUS | exit |
  |---|---|---|
  | serial listed but cannot be opened (another program has it) | `E_BUSY` | 75 |
  | not connected / open or setting refused | `E_DEVICE` | 69 |
  | rate not in the device's list | `E_UNSUPPORTED` | 64 |
  | lock held by another worker | `E_BUSY` | 75 |

  Replay-only arguments (`--file`, `--origin-hz`, `--paced`, …) exit 64. So does a build
  without libairspyhf.

## Samples

- SAMPLES frames are `CF32` and `--frame-samples` long. `freq_origin_hz` is the centre and
  `sample_rate_hz` the capture rate.
- `first_sample_index` counts delivered samples within the generation, as in replay. Losses do
  not advance it; `gap_samples` reports them.
- The driver's callback thread copies each block into a 512-slot single-producer ring and
  wakes the worker loop through an eventfd. The worker never blocks the driver.
- **Losses are flagged at the frame where they happen.** Staged samples from before a loss go
  out first as a shorter frame:

  | loss | flags on the next frame |
  |---|---|
  | `dropped_samples` reported by libairspyhf | `DISCONTINUITY \| GAP_KNOWN \| OVERFLOW`, `gap_samples` = the count |
  | ring full (worker loop behind the driver) | `DISCONTINUITY \| GAP_KNOWN` |
  | data socket backlog over 8 MB (control service not reading): the whole frame and its DSP are dropped | `DISCONTINUITY \| GAP_KNOWN` |

- The first 2 driver blocks after start are discarded, like after a retune.

## SET_CAPTURE (retune, rate change)

- **ACK:** `E_OUT_OF_RANGE` unless 1 ≤ centre ≤ 4 294 967 295 Hz. `E_UNSUPPORTED` unless the
  rate is in the device's list. `E_BAD_MESSAGE` for bad flags. The device is not touched when
  the ACK is a refusal.
- **Then:**
  1. A new tuning epoch starts **before** the driver call. Blocks captured before or during the
     change carry an older epoch and are discarded.
  2. The first **2 blocks** of the new epoch are discarded too. The step lands up to about one
     block after the call, and about 1490 samples (at 192k) are lost silently around it
     (Window 1b, H5).
  3. A centre change is `airspyhf_set_freq` while streaming. A rate change is
     stop → `set_samplerate` → start, because changing the rate while streaming was not
     measured.
  4. `capture_gen` increments. The next frame carries `NEW_GEN | DISCONTINUITY` with index 0,
     and the spectrum and every channel restart as in replay.
- **APPLIED** reports the centre and rate set, with `verified = 0`. libairspyhf has no frequency
  getter, so this is "set and accepted by the driver", never `driver_readback`.
- **A driver error after ACK** gives FAILED/`E_DEVICE`, then STATUS FAILED and exit 69. The
  device state is unknown, so the worker leaves, and the control service restarts it with
  every control set again.

## SET_HW / HW_APPLIED (new in worker_ctl v1)

| field | values | acts |
|---|---|---|
| `hf_agc` | 0 off, 1 on | — |
| `hf_agc_threshold` | 0 low, 1 high | with AGC on |
| `hf_lna` | 0 off, 1 on | — |
| `hf_att` | 0..8 (≈ 5 dB per step measured) | with AGC off |

- **Request:** `flags` selects the fields to change; the others keep their values.
- **ACK:** `E_UNSUPPORTED` on replay sources. `E_OUT_OF_RANGE` for a bad value.
  `E_BAD_MESSAGE` for no or unknown flags, or non-zero reserved bytes. The device is untouched
  when the ACK is a refusal.
- **Then:** all four controls are set (not only the changed ones), and **HW_APPLIED** reports
  every value now in effect, with `verified = 0`.
- **Generations:** a gain change does not start a new capture generation.
- **A driver error** is handled as in SET_CAPTURE: FAILED, then exit 69.

## Stopping and device loss

- **Device loss.** The loop checks every 100 ms. If `airspyhf_is_streaming` is 0, or no driver
  block has arrived for 1 s, the worker sends STATUS FAILED/`E_DEVICE` and exits 69 **without
  calling `airspyhf_stop`/`close`**. Those hang after an unplug (Window 1b, H7); the OS releases
  USB with the process.
- **Normal stop** (STOP, SIGTERM, control socket closed): `airspyhf_stop` then `close`, under a
  3 s watchdog. If they do not return, the worker exits 71.
