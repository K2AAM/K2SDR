# Milestone 1 — First live receiver: design note

Status: **proposed — awaiting Adam's approval. No M1 code is written until it is approved.**
Date: 2026-10-26. Scope source: outline §12 (Milestone 1), §3, §4, §5 (baseline audio
only), §10 (recording, mobile), §11, §13.

## 1. Scope

**In (outline Milestone 1):** Airspy HF+ Discovery as a live receiver; AM, USB, LSB,
CW, NFM, WFM listening; spectrum + waterfall; tuning (click and typed, integer Hz);
compact RIG; basic recording (WAV); first overnight run.

**Completion evidence (outline):** actual RF and recorded-signal tests; first
overnight run; audio continuity and format measured.

**Out (later milestones):** SDRplay and antenna switching (M2), RX888 (M2),
RNNoise (M2), decoders/map (M3), ALE (M4), SAM and DATA modes (ledger entries stay
unavailable until done properly), MFA and any non-loopback exposure.

## 2. Hardware plan: the Airspy is OWRX's today

The Airspy HF+ is the only SDR on the Optiplex and OWRX uses it continuously. M1
needs it only in **short, scheduled windows that Adam runs**
(`docs/ops/owrx-handoff.md`). Development between windows uses **real IQ recorded
during the first window** and replayed, so windows stay few and short.

### Window 1 — measure and record (~45 min, OWRX paused for that device)

Adam: disable the Airspy in OWRX (or stop OWRX), add `k2dev` to `plugdev`, log
k2dev out and in. Then one command (`tools/airspyhf_probe`, new, read-mostly) on
the Optiplex measures and records, while Adam listens to nothing in particular:

| # | Measurement | Why it matters |
| --- | --- | --- |
| H1 | `airspyhf_lib_version`, Debian package version, firmware `version_string_read`, serial | Pin what we build against; Debian's libairspyhf appears older than upstream 1.8.1 |
| H2 | `airspyhf_get_samplerates` list; `is_low_if` per rate | Choose capture rates from what the device reports, not from memory |
| H3 | `get_att_steps`, `get_frontend_options`, bias-tee count | Which gain/attenuator/AGC controls exist (HF AGC, threshold, att 0–8, LNA) |
| H4 | Sustained streaming 10 min per candidate rate: `dropped_samples`, callback cadence, CPU | Pick the default rate; baseline for the overnight run |
| H5 | Retune settle: samples until a known carrier is stable after `set_freq` | How many samples to discard after a retune (frames are already generation-tagged) |
| H6 | Frequency accuracy against WWV/CHU (10 MHz, 7.85 MHz…) and `get_calibration` | Calibration value and its status shown in the UI |
| H7 | Unplug/replug during streaming (Adam pulls the cable once) | Error path and recovery behaviour |
| H8 | **Record SigMF IQ**: FT8 7.074 MHz, 40 m SSB, MW AM broadcast, WWV, FM broadcast | Real-signal replay fixtures for all of M1 development and CI |

Then Adam removes `plugdev`, re-enables the device in OWRX, and checks OWRX audio.
Results go to `docs/ops/airspyhf-measurements-<date>.md`; every later M1 default
cites a measurement row, not a guess.

**Not done without separate approval:** opening the Airspy while OWRX holds it
(to learn what "busy" looks like). It is useful for an honest `external_busy`
state, but it could disturb OWRX's stream; if wanted, it is done at the end of a
window with Adam listening and abort-on-any-glitch.

### Windows 2+ — live validation

Short windows for: first live audio, the overnight run (outline gate), and final
acceptance. Each follows the handoff procedure and the coexistence snapshot.

### Licensing check (done)

libairspyhf's library sources (`airspyhf.c`, `iqbalancer.c`, `airspyhf.h`) carry a
BSD license; only its command-line tools and CMake files are GPL-2.0. Linking the
library is compatible with any project license we later choose.

## 3. Architecture changes

### 3.1 Worker: a second source

`ksdr-worker --source airspyhf --serial <hex>` via **native libairspyhf**
(outline §4: evaluate native vs. the existing Soapy route; native first because
it exposes `dropped_samples`, calibration and the HF AGC/attenuator controls
directly, and adds no Soapy layer). Soapy remains the comparison path if H4 or H7
show problems. The library's callback thread hands blocks to the worker's loop via
a lock-free single-producer ring; drops are counted and become frame
`DISCONTINUITY` + `GAP_KNOWN` with the library's `dropped_samples`.

Device lock (`flock`) is still taken before `airspyhf_open_sn`. The worker refuses
to start unless the device's serial matches the configured one.

### 3.2 DSP in the worker (outline §11: DSP stays out of Python)

Per receiver: one FFT for spectrum/waterfall. Per listener channel: mix to
baseband → decimating low-pass → demodulator → AGC → 12 kS/s audio.

| Block | M1 approach |
| --- | --- |
| FFT | One per receiver, Hann window, 2048–4096 bins, ~10–20 frames/s, averaged, quantized to uint8 dB |
| Channel extraction | NCO mix + polyphase decimating FIR |
| AM | Envelope, DC removal |
| USB/LSB | Complex filter selecting the sideband, real part |
| CW | USB with configurable BFO offset (default 700 Hz) and narrow filter |
| NFM | Quadrature discriminator, de-emphasis off |
| WFM | Discriminator at wider bandwidth, 75 µs de-emphasis, mono |
| AGC | Fast / Medium / Slow / Off, per channel (outline §3), separate from hardware gain |

**DSP library (decision needed, see §6):** use **liquid-dsp** (MIT, packaged in
Debian; mature filters, NCOs, demodulators, AGC), which matches the outline's
"established DSP libraries". Its FFT backend must be checked on the Optiplex: if
Debian's build links FFTW (GPL), we either accept that once the project license
is chosen or use liquid-dsp's internal FFT / vendored pocketfft (BSD-3) for the
spectrum. Fallback if liquid-dsp is unsuitable: our own small blocks + pocketfft.

### 3.3 Protocol additions (additive within v1)

- **frame_v1 kinds:** `SPECTRUM` (uint8 dB bins + FFT params) and `AUDIO`
  (int16 PCM, 12 kS/s mono, per channel id + `channel_gen`). Same 80-byte header.
- **worker_ctl:** `CHANNEL_OPEN/TUNE/CLOSE` (offset Hz, mode, passband edges,
  AGC), `SET_HW` (attenuator, LNA, HF AGC, threshold, calibration). Same
  ACK → APPLIED/FAILED lifecycle; readback in APPLIED.
- **control v1:** `channel.*` become live; `receiver.set_hw_gain` gets its schema
  from H3; new binary WebSocket stream per session for spectrum + own audio.
- Channel tuning inside the capture span needs only `listen` and no lease; a
  hardware retune (center/rate) still needs the lease (outline §3, §11).

### 3.4 Audio to the browser

**M1 format: int16 PCM, 12 kS/s mono over the WebSocket (~192 kbit/s per
listener)**, played by an AudioWorklet with a jitter buffer. Measured, not
assumed: underruns, latency, and bandwidth on desktop and a real iPhone. A
compressed codec (IMA-ADPCM or Opus) is a later, measured choice; Opus decode on
iPhone Safari is not assumed. Volume and mute are browser-side; the server
stream is pre-volume (decoder tap point preserved for M3).

### 3.5 Browser UI

Svelte + TypeScript + Vite (outline §11), built in CI; the built files are
served by the control service, so the Optiplex needs no Node at runtime (Debian
13 ships Node 20.19 for local builds). M1 UI = outline §3 essentials only:
thin header, spectrum + waterfall (canvas, auto levels + manual override),
compact RIG (frequency with unit-explicit entry using the shared parser vectors,
mode, passband, step, S-meter in dBFS with calibration status, volume/mute, AGC,
record). Tap-to-resume audio on iPhone after suspend. Replaces the M0 status page
for signed-in users; admin pages stay server-rendered.

### 3.6 Recording

Server-side WAV of a channel's clean demodulated audio (outline §10: reliable
first; the browser can be suspended). Filename `<UTC>_<frequency_hz>_<mode>.wav`,
needs the `record` permission, bounded storage (admin-set cap, oldest-first
refusal, visible disk-full state). Downloads via authenticated HTTP.

## 4. Testing strategy

- **CI (no hardware):** replay the H8 recordings through the full chain;
  golden tests per mode (e.g. AM broadcast audio correlates with a reference
  demodulation; FT8 recording yields audio with tones at the expected offsets;
  WWV carrier at the expected bin). Synthetic fixtures keep exact-bin tests.
- **Sanitizers:** worker DSP under ASan/UBSan as today.
- **Hardware tests:** only in windows, scripted, results committed as evidence.
  Nothing is called "live validated" without such a record.

## 5. M1 acceptance checklist

| # | Check | Outline |
| --- | --- | --- |
| B1 | H1–H8 measured and recorded; defaults cite them | §4 validation, §12 |
| B2 | Each mode demodulates the recorded real signals (CI golden tests) | §12 recorded-signal tests |
| B3 | Live audio from the Airspy in every M1 mode, desktop + iPhone (window) | §12 actual RF |
| B4 | Overnight run: dropped samples, audio underruns, CPU, memory, disk recorded | §12 overnight run |
| B5 | Frequencies identical in UI, state, recording filename/metadata | §13 |
| B6 | Retune: stale audio/spectrum of the old generation never shown | §13 |
| B7 | Browser suspend/reconnect on iPhone: audio resumes with a tap, server state intact | §13 |
| B8 | USB unplug: visible failure, bounded recovery, no hang | §13 |
| B9 | Recording: correct WAV, retention cap obeyed, disk-full visible | §13 |
| B10 | OWRX coexistence trial passes before and after every window | M0 A13 |

## 6. Decisions for Adam

1. **Native libairspyhf first**, Soapy as fallback? (Recommended.)
2. **liquid-dsp** for DSP, subject to the FFTW check on the Optiplex? (Recommended.)
   Alternative: own blocks + pocketfft.
3. **int16 PCM @ 12 kS/s** for M1 audio, codec decided later by measurement?
4. **Server-side recording** first (vs. browser-side)?
5. **Window 1** as described (~45 min, Airspy away from OWRX, `plugdev` granted
   to k2dev for the window only). When suits you?
6. Optional "open while OWRX holds it" test — yes, no, or later?

## 7. Work order after approval

1. `tools/airspyhf_probe` + Window 1 (measurements, recordings) — blocks the rest.
2. Worker DSP on replay (FFT, channels, demods, AGC) with real-recording golden tests.
3. Protocol additions + control service channels and binary streams.
4. Airspy source in the worker (built and unit-tested without hardware; first live run in Window 2).
5. Svelte UI (spectrum, waterfall, RIG, audio worklet).
6. Recording.
7. Window 2: live acceptance; Window 3: overnight run.

Each step is its own PR with CI green before merge, as in M0.

## 7a. Things on the Optiplex I need checked (read-only, k2dev)

```bash
apt-cache policy libairspyhf-dev libliquid-dev nodejs
apt-cache depends libliquid1            # does Debian's liquid-dsp link libfftw3?
dpkg -l | grep -i -E "airspy|soapy"      # what OWRX already uses
```
