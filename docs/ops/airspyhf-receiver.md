# Configuring an Airspy HF+ receiver (M1)

The service can run the Airspy HF+ Discovery as a live receiver. **Only during a hardware
window that Adam has scheduled.** OpenWebRX+ normally owns the device, and the `k2dev`
account is not in `plugdev` outside windows.

```toml
[[receivers]]
id = "hfplus"
label = "Airspy HF+ Discovery"
driver = "airspyhf"
serial = "XXXXXXXXXXXXXXXX"        # 16 hex digits, as `airspyhf_info` / the probe's `info` print it
center_hz = 7100000                # integer Hz
rate_hz = 384000                   # low-IF default (Window 1b: clean for 10 min, 2.7 % of a core)
freq_range_hz = [10000, 30000000]  # safe range for the centre: an admin choice, see below
# sample_rates_hz = [384000, 192000]   # optional subset of the rates the device reported
# Gain on open (approved starting values, 2026-10-26); change at runtime with receiver.set_hw_gain:
# hf_agc = true
# hf_agc_threshold = "low"
# hf_lna = true
# hf_att = 0
```

**Notes:**
- **`freq_range_hz` is required and is not a device specification.** Published coverage
  figures for the HF+ Discovery disagree at the low end, so the service never assumes one. M1
  is about HF, and 10 kHz–30 MHz stays inside every published figure.
- **Defaults:** `sample_rates_hz` defaults to the seven rates the device reported in Window 1
  (912k … 192k). Others are refused.
- **Startup:** the worker opens the device by serial, after the per-device lock
  (`airspyhf-<serial>`). It sets every control on open, because the device keeps the previous
  program's settings (Window 1b).
- **If the device is busy or unplugged,** the receiver shows `external_busy` or `failed` with a
  retry time and comes back by itself (`docs/protocol/control-v1.md`, "Live receivers").
  Without USB access (outside a window) it retries quietly every 30 s, so leave the entry out
  of the config outside hardware windows.
- **When the LZ1AQ loop replaces the W6LVP,** repeat the H3 gain sweep before trusting the
  gain defaults on it.

**Without hardware:** `control/tests/test_web_airspyhf.py` runs this configuration against the
mock worker (`build/worker/ksdr-worker-airspyhf-mock`). That run includes an unplug and the
recovery.
