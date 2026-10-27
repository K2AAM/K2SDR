# M1 worker DSP — parameters and behaviour

Implements design §3.2 in `worker/src/dsp/` (library `k2dsp`, linked against the pinned
liquid-dsp 1.8.3). The worker runs it on every capture block and sends SPECTRUM/AUDIO frames;
listener channels are opened with worker_ctl CHANNEL_* (`docs/protocol/worker-dsp-v1.md`). Tests: `control/tests/test_dsp_pipeline.py` (via `ksdr-dsp-run`), also
under ASan/UBSan (`make test-worker-san`).

## Spectrum (one per receiver)

| | |
|---|---|
| FFT | `fft_n` 16..65536 (M1: 2048–4096), periodic Hann window, liquid internal FFT |
| Frames | `frames_per_s`; frame k ends at sample `k * rate / fps` (exact integer arithmetic) |
| Averaging | the most recent max(1, ⌊period / fft_n⌋) non-overlapping blocks, power-averaged |
| Order | FFT-shifted: bin 0 = −rate/2, bin n/2 = centre |
| Level | dBFS referenced to a full-scale complex tone centred in a bin (= 0 dBFS). White noise of power σ² reads 10·log10(σ²·Σw²/(Σw)²) per bin |
| Quantization | uint8: dBFS = −127.5 + 0.5 · q (−127.5 … 0 dBFS), clamped |

## Channel (one per listener)

NCO mix (phase-continuous retune) → liquid `msresamp_crcf` (70 dB) → complex FIR passband
(Kaiser, 70 dB, transition 10 % of width, 100–2000 Hz) → demodulator → AGC → int16 PCM at
12000 S/s mono.

| Mode | Channel rate | Default passband (Hz, relative to the tuned frequency) | Demodulator |
|---|---|---|---|
| AM | 12 kS/s | −4500 … 4500 | envelope, DC blocker |
| USB | 12 kS/s | 300 … 2700 | real part |
| LSB | 12 kS/s | −2700 … −300 | real part |
| CW | 12 kS/s | −250 … 250 around the carrier, heard at BFO (default 700 Hz, 100–3000) | real part |
| NFM | 24 kS/s | −6000 … 6000 | `freqdem`, full scale at 5 kHz deviation; audio resampled to 12 kS/s |
| WFM | 192 kS/s | −90000 … 90000 | `freqdem` at 75 kHz, 75 µs de-emphasis, mono; audio resampled to 12 kS/s |

- Offsets and passband edges are integer Hz.
- A config is refused, with a reason, if:
  - the capture rate is below the channel rate;
  - the passband is outside ±(rate/2 − 5 %) of the channel;
  - the channel passband leaves the capture span;
  - lo ≥ hi;
  - the BFO is out of range.
- **S-meter:** channel power inside the passband, before AGC, 100 ms average. dBFS: a
  full-scale tone in the passband reads 0.
- Outputs do not depend on input block size, and are deterministic on a given build host.
  liquid's SIMD path is chosen on the build host, so outputs are not bit-identical across
  machines, and tests compare properties, not hashes.
- Audio is good to about 4.5 kHz at 12 kS/s (resampler roll-off: −0.5 dB at 4 kHz, −2 dB at
  5 kHz).

## AGC (per channel, separate from hardware gain)

Own implementation (`dsp/agc.cpp`), replacing liquid's `agc_rrrf`. liquid's AGC overshoots
+27 to +40 dB on a sudden strong signal (`docs/ops/liquid-dsp-1.8.3.md`).

- **Look-ahead:** 5 ms (60 samples). The envelope sees the undelayed signal through a sliding
  maximum, and the output is delayed by 5 ms, so the gain is already down when a peak arrives:
  **no overshoot** on a +40 dB step (test: < 1 dB).
- **Attack** 0.5 ms; **hang**, then exponential **decay**; gain = target / envelope, capped at
  `max_gain_db`.
- Advanced parameters (`AgcConfig`): target, max gain, attack, look-ahead, hang, decay, manual
  gain.

| Mode | Hang | Decay | Back within 3 dB after a 40 dB drop (measured) |
|---|---|---|---|
| Fast | 0 | 50 ms | 0.27 s |
| Medium | 100 ms | 250 ms | 1.47 s |
| Slow | 500 ms | 750 ms | 4.61 s |
| Off | — | — | fixed `manual_gain_db`, no added latency |

Defaults: target −6 dBFS peak, max gain 70 dB. int16 clipping is counted per channel.

## Golden checks on real recordings (design §4)

- WWV carrier in the expected spectrum bin (±1), > 30 dB above the median.
- AM on WWV: the 600 Hz tone and the 100 Hz time-code subcarrier, each > 20 dB above the
  audio median.
- USB on the FT8 slot: energy at the decoded stations' audio offsets; > 55 dB down above the
  passband; no clipping.
