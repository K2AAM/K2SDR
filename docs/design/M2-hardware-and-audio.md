# Milestone 2 — Hardware and audio: design note

Status: **proposed, 2026-10-31**, for Adam's approval (§7).
Scope source: outline §12 (Milestone 2), §3, §4, §5, §11, §13. Inputs: Adam's hardware facts
(2026-10-30/31), the PhantomSDR-Plus assessment (`phantomsdr-plus-assessment-2026-10-31.md`) and
the M1 CPU split (`docs/ops/cpu-split-2026-10-30.md`).

## 1. Scope

**In (outline Milestone 2):**
- RX888 prototype with a selected backend, and its throughput measured.
- SDRplay with antenna controls.
- RNNoise.
- AGC and gain controls.

**Completion evidence (outline):**
- A real device matrix.
- Controlled port-switch tests.
- An RNNoise comparison, plus proof that decoder audio is isolated from it.
- Measured RX888 throughput.

**Out:** decoders and the map (M3), ALE (M4), public exposure hardening (M6). M2 keeps the
service on loopback behind Tailscale Serve, as in Window 3.

## 2. The hardware situation (Adam, 2026-10-30/31)

**Optiplex:**
- The **RX888 MkII** (serial `0009060505481B1E`) is now the only SDR. It works at USB 3
  SuperSpeed after a cable swap.
- The Airspy is off the antenna until the multicoupler is fixed.
- **PhantomSDR-Plus** runs the RX888 publicly through `phantomsdr-receiver.service`. It serves
  port 8073, and its DRM helper uses 8075, our port. OWRX+ is disabled.
- Our windows: Adam stops PhantomSDR first, so port 8075 and the RX888 are free.

**Remote site (about 50 miles away):**
- An **SDRplay RSPdx2** with 3 antenna ports, on Adam's HP EliteDesk: Debian, OWRX+, Cloudflare
  tunnel, Tailscale SSH.
- SDRplay support therefore means a **second installation** of this service on that host, with
  a local USB device. Adam's partner uses all three ports for VHF/UHF/HF.

**Users:** Adam, the partner, a few public listeners.

## 3. The RX888 changes the DSP architecture

The RX888's HF path is a 16-bit ADC sampling **real** (not IQ) signal, at up to about 64 MS/s
for 0–30 MHz. That is 128 MB/s over USB. It also has a VHF tuner (R828D).

The M1 channel design does not scale to this:
- Every channel mixes and resamples the full input stream on its own.
- That costs about 6.3 % of an Optiplex core per channel at 384 kS/s.
- At 60 MS/s that is impossible: a single channel would need more than a core.

The standard answer, used by PhantomSDR and wideband WebSDRs, is to do the expensive work once,
for everyone.

### 3.1 Wideband front end (worker)

- **One shared real FFT** of the full input with 50 % overlap (overlap-save), feeding two things:
  1. **The spectrum.** Rows are cut from the FFT bins for the window each session shows.
     Zoom becomes **server-side** per session: the 30 MHz band with 2048 bins would be 15 kHz
     per bin, which is useless for tuning. Today's client-side zoom becomes the fine part on top.
  2. **Channels by fast convolution.** Each channel takes the bins around its frequency,
     multiplies by its filter in the frequency domain, takes a small inverse FFT, overlap-saves,
     and lands at a low rate (e.g. 24–48 kS/s). The existing demodulators and AGC then run as in M1.
- **The cost moves:** a big FFT once, plus a small inverse FFT per channel. That makes a channel
  cheap.
- **Budget:** `make dsp-bench` gains a wideband case. On the Optiplex, the FFT plus N channels is
  measured at 32 and 64 MS/s before the design is fixed. OpenCL is an option afterwards, only if
  the CPU is not enough.
- **The Airspy keeps working:** narrowband (384 kS/s) sources still use the M1 path. The front
  end is chosen per source.

**Generations, leases and integer Hz are unchanged:**
- Each RX888 setting change (sample rate, HF/VHF switch, VHF tuner frequency, attenuator, VGA)
  is a capture change under the lease.
- On HF there is no tuning at all: the whole 0–30 MHz is always captured, so listeners tune
  freely within it and no lease is needed for that.

### 3.2 RX888 backend: decision needed (§7.2)

| Option | What | For | Against |
|---|---|---|---|
| A | Run `rhgndf/rx888_stream` (Rust, GPL-3.0) as a separate process and read its sample stream from a pipe | Works today on the Optiplex (PhantomSDR uses it); zero device code to write; a separate process keeps its GPL-3 apart from our code (aggregation, not linking) | Needs a Rust toolchain to build. Its error and recovery behaviour (unplug, USB errors) has to be measured. Only a raw pipe: settings are fixed at start, so every setting change means a restart |
| B | Our own small libusb source in the worker: upload the FX3 firmware, set clock, gain and attenuation, stream bulk transfers | Fits the worker exactly (generations, unplug handling, watchdogs as for the Airspy). One process | We write and test the device code (the protocol is small and documented by several open implementations). The **firmware image's licence** must allow us to ship or load it |
| C | libsddc / SoapySDDC | Library API | Less actively maintained. Pulls in the Soapy layer for one device |

**Recommendation:**
- **A for the measurement window and the first prototype**, so everything can be measured with
  no device code.
- **Then B** if the firmware licence checks out: it gives the same reliability rules as the
  Airspy source.
- If the firmware licence does not check out, stay on A.

### 3.3 RX888 measurement window (Window R1, ~45 min)

Run with PhantomSDR stopped, scripted like Window 1:

| # | Measurement | Why |
|---|---|---|
| R1 | Versions: rx888_stream commit, firmware image hash, USB link speed | Pin what we use |
| R2 | Sustained streaming at 32, 50, 64 MS/s, 10 min each: lost transfers, CPU of the streamer | Choose the default rate |
| R3 | HF gain and attenuation steps on WWV/noise: dB per step, overload point | Gain defaults (outline: gain controls) |
| R4 | Dither and randomizer on/off | Spur and noise effects |
| R5 | Clock accuracy against WWV | Frequency calibration |
| R6 | VHF mode: tuner range, settle time after a retune, NOAA and FM | VHF support; the NFM real clip (also closes M1 B2) |
| R7 | Unplug and replug while streaming | Recovery behaviour (B8-like) |
| R8 | Recordings: short wideband HF captures, decimated clips for CI fixtures | Development by replay, as in M1 |

## 4. RNNoise

- **Where:** in the browser, in the audio worklet, after the server has sent the audio. Our
  architecture already puts decoder audio upstream of RNNoise, mute and volume (outline
  non-negotiable): decoders and recordings use the server's audio, which RNNoise never touches.
- **Source:** upstream xiph/rnnoise (BSD-3) compiled to WebAssembly ourselves. Not PhantomSDR's
  wrapper.
- **Rate:** RNNoise works at 48 kHz in 10 ms frames, so the worklet upsamples the 12 kS/s audio
  to 48 kHz and back.
- **Evidence:**
  - A/B listening plus an objective comparison on recorded clips: SNR and loudness before and
    after.
  - An automated test that decoder and recorded audio are bit-identical with RNNoise on and off.

## 5. Gain controls and AGC

- **Channel AGC:** already done in M1 (own look-ahead AGC, off/fast/medium/slow, manual gain).
- **Hardware gain:** M2 adds the RX888's hardware gain (HF attenuator and PGA, VHF LNA and VGA),
  as leased controls. The `receiver.set_hw_gain` pattern from the Airspy is generalised to a
  per-device schema, so the UI shows each device's real controls and steps.
- **Defaults:** from R3, and from H3 for the Airspy.

## 6. SDRplay (second site)

**Licence (needs Adam's OK, §7.4):**
- The **SDRplay API (v3)** is closed source, with its own licence.
- It is installed on that host as a system service, and our worker links against it or talks
  to it through SoapySDRPlay3.
- We would not redistribute it: Adam installs it from SDRplay.

**Antenna ports:**
- The port is **global, leased state**, never changed as a side effect of tuning or switching
  bands (outline §13).
- A port switch is a capture change: new generation, settle discard.
- The "controlled port switch tests" use recorded signals per port plus a short window with
  Adam's partner informed.

**Order:** RX888 first. SDRplay follows, with the same host rules: OWRX+ there keeps running,
and windows are scheduled by Adam.

## 7. Decisions for Adam

1. **Order:** RX888 (window R1, then the wideband front end), then hardware gain controls, then
   RNNoise, then SDRplay. (Recommended.)
2. **RX888 backend:** A (rx888_stream as a separate process) for R1 and the prototype; B (own
   libusb source) if the firmware licence allows. (Recommended.)
3. **PhantomSDR-Plus:** not forked. Ideas only, no code (assessment of 2026-10-31). (Adam
   deferred to this recommendation.)
4. **SDRplay API:** OK to depend on SDRplay's closed API on the EliteDesk (installed by Adam,
   never redistributed by us)?
5. **Project licence:** still open. rx888_stream and PhantomSDR are GPL-3, the SDRplay API is
   proprietary, liquid-dsp is MIT, RNNoise is BSD-3.
   - GPL-3 for this project would be consistent with A and would make borrowing GPL-3 code
     possible (with its terms).
   - A permissive licence keeps more options open but rules that out.
   - This is Adam's decision. Nothing in M2 forces it before backend B.
6. **Window R1:** about 45 minutes, PhantomSDR stopped, whenever convenient.

## 8. Work order after approval

1. `tools/rx888/` window script (around rx888_stream) and Window R1.
2. Wideband DSP on replayed RX888 recordings: FFT, server-side zoom, fast-convolution channels,
   `dsp-bench` on the Optiplex.
3. RX888 source in the worker (backend A, then B if chosen), with mock-device tests.
4. Hardware gain controls (per-device schema), UI.
5. RNNoise (wasm worklet), with the decoder-isolation test.
6. SDRplay on the EliteDesk.

Each step is its own PR, merged with CI green, as in M0/M1.
