# liquid-dsp 1.8.3 without FFTW — build, verification, measurements

Decision (Adam, 2026-10-26): Debian 13's liquid-dsp 1.7.0 depends on FFTW (GPL), so the worker
uses liquid-dsp **1.8.3 built from pinned source without FFTW**, linked statically, with
liquid's internal FFT. The project license stays pending.

## Pin and build

| | |
|---|---|
| Source | https://github.com/jgaeddert/liquid-dsp, tag v1.8.3, commit `10041f70cebbe3b97887e75bb41e48b73dda1b23` (2026-09-26) |
| Verification | `tools/fetch_liquid.py`: shallow git fetch of that commit, commit id checked, then SHA-256 over every file's path, type and SHA-256 (`0ce25baae2ab399a…`). Re-checked on every build; a modified file triggers a fresh fetch. Matched independently against a full clone at the tag. |
| Build | `tools/build_liquid.sh` → `build/third_party/liquid-dsp` (static `libliquid.a`, headers, LICENSE, `k2-build-info.json`). Nothing outside the build tree is touched. |
| FFTW off | `FIND_FFTW=OFF` (the option name in this revision; it gates `find_package(fftw3f)` and `-lfftw3f`) and `-DLIQUID_FFTOVERRIDE=1` (the source-level switch). With CMake, v1.8.3 never defines `HAVE_FFTW3_H`, so the internal FFT is selected either way; both are set so no future change silently flips it. The build script also refuses a CMake cache that located FFTW. |
| No fallback | The worker imports `libliquid.a` by absolute path (no `find_package`/`find_library`), includes the pinned header (`-isystem`, ahead of `/usr/include`), and `worker/src/liquid_pin.hpp` fails compilation unless the header is 1.8.3 (Debian ships 1.7.0). `ksdr-worker --version` reports the runtime library version. |

## Verification of the dependency closure

`tools/check_dsp_deps.py` (also `make dsp-deps`, and a test in `make check`):

- **liquid build**: build info matches the pin; the CMake cache has `FIND_FFTW=OFF` and no FFTW
  path; `libliquid.a` has no undefined `fftw*` symbol. Its only fftw-named symbol is liquid's own
  `liquid_fftwf_cleanup_wrapper`, whose FFTW call is compiled out.
- **each binary** (`ksdr-worker`, `ksdr-dsp-check`):
  - the dynamic `NEEDED` entries are only the C/C++ runtime;
  - no `fftw` symbol in the symbol table;
  - the linker map (`-Wl,-Map`) has no FFTW input, and exactly one `libliquid` input, which is
    our archive;
  - the link commands mention no FFTW;
  - the runtime version is 1.8.3.

The symbol-table and map checks are what catch a statically linked FFTW; `ldd` alone cannot.

**With FFTW and the distribution's FFTW-linked liquid-dsp installed** (`libfftw3-dev`,
`libliquid-dev`; in CI on Debian 13, and locally on Ubuntu 24.04), a from-scratch build passes
every check.

**Negative controls**, run once and kept as a test where practical:
1. *liquid built with `FIND_FFTW=ON`:* the check fails on the CMake cache. The binaries were
   still FFTW-free, because the source path stays internal.
2. *liquid built with the FFTW code path forced (`HAVE_FFTW3_H=1`), FFTW linked statically:*
   - `NEEDED` is clean (what `ldd` would show);
   - the check fails on the archive (6 undefined `fftwf_*`), the binary symbols, the linker map
     and the link command.
3. *Permanent test:* a fake static `libfftw3f.a` linked into stand-in binaries
   (`test_checker_catches_statically_linked_fftw_that_ldd_cannot_see`).

## Correctness (control/tests/test_liquid_dsp.py, 25 tests)

`ksdr-dsp-check` (`worker/dsp/dsp_check.cpp`) runs liquid's blocks in the M1 chain from
design §3.2:

- NCO mix
- multi-stage polyphase resampler (`msresamp`) to 12 kS/s (24 kS/s for NFM, 192 kS/s for WFM)
- complex FIR sideband/channel filter
- envelope + DC blocker (AM)
- `freqdem` (NFM, WFM + 75 µs de-emphasis)
- `agc_rrrf`

| Check | Result |
|---|---|
| FFT vs numpy, N = 1024, 2048, 3000, 4096, 8192 | max error < 2e-5 of peak |
| Channel 384k → 12k, tone 20 kHz outside the channel | > 60 dB rejected; output length exact ± 64 |
| Fractional decimation (650k) and 192k, 912k | correct tone and length |
| USB vs LSB | wrong sideband > 50 dB down |
| CW | carrier → BFO pitch (700, 600 Hz) |
| AM | 1 kHz tone recovered, DC blocked, 2nd harmonic > 30 dB down |
| NFM | output proportional to deviation (±5 %) |
| WFM | 75 µs de-emphasis within 0.3 dB of theory (1 vs 3 kHz) |
| Audio band at 12 kS/s | flat to 2 kHz; −0.5 dB at 4 kHz, −2 dB at 5 kHz, −3.3 dB at 5.5 kHz → **audio is good to ~4.5 kHz** |
| AGC | quiet (−40 dB) and loud input leave at the same RMS; fast < medium < slow |
| **Real WWV (AM)** | audio shows WWV's 600 Hz tone (+29 dB) and its 100 Hz time-code subcarrier (+27 dB) at the exact frequencies |
| **Real FT8 slot (USB)** | audio spectrum correlates > 0.95 with an independent numpy USB demodulator; nothing above the passband (−91 dB); the strongest decoded stations stand out at their decoded audio frequencies |

Not covered by a real recording: NFM/WFM (the FM-band recording stays on the Optiplex; FM is
tested synthetically only).

## AGC: finding and proposal

Measured at 12 kS/s (settling = stays within 3 dB after a ±40 dB step):

| loop bandwidth | +40 dB step | −40 dB step | overshoot on +40 dB |
|---|---|---|---|
| 3e-2 (fast) | 0.04 s | 0.01 s | **+27 dB** |
| 1e-2 | 0.12 s | 0.04 s | +32 dB |
| 5e-3 (medium) | 0.23 s | 0.07 s | +34 dB |
| 1e-3 (slow) | 1.16 s | 0.37 s | +39 dB |

liquid's AGC is a single symmetric loop. A sudden strong signal comes out up to about 40 dB
too loud until the loop catches up, which is unacceptable for listening.

**Proposal for the DSP PR:** keep `agc_rrrf` as the slow level loop and add a small
attack/decay stage, so the gain drops within milliseconds on a peak and recovers at the
Fast/Medium/Slow rate. If needed, also add a hard limiter at the output. It is implemented and
tested in the worker DSP PR, not here.

## Performance

`make dsp-bench` (`ksdr-dsp-check bench`): a spectrum frame is Hann window + FFT + |X|² + dB
+ uint8. "Channel" is the full chain at the given capture rate, per listener.

| | cloud container (Xeon 2.1 GHz, AVX-512) | **Optiplex** (Debian 13; build detected AVX2, no AVX-512), 2026-10-26 |
|---|---|---|
| FFT 2048 / frame | 29 µs / 48 µs | **20 µs / 31 µs** |
| FFT 4096 / frame | 59 µs / 93 µs | **44 µs / 67 µs** |
| 4096 at 20 fps | 0.19 % of a core | **0.13 %** |
| 4096 transforming every sample at 912k | 2.1 % | **1.5 %** |
| channel @ 192k | 0.8–0.9 % (wfm 2.7 %) | **0.6 % (wfm 1.8 %)** |
| channel @ 384k (am/usb/lsb/cw/nfm/wfm) | 1.9–2.2 % (wfm 3.4 %) | **1.1–1.2 % (wfm 2.1 %)** |
| channel @ 912k | 4.6–5.5 % (wfm 7.1 %) | **2.6–2.7 % (wfm 3.6 %)** |

The internal FFT is not a bottleneck at the planned 2048–4096 sizes. Per-channel cost is
dominated by mixing and resampling at the full capture rate. That is fine for M1's few
listeners; a shared first decimation stage is the lever if many channels are needed later.
**Accepted on the Optiplex numbers.** At the likely default of 384 kS/s:
- capture: 2.7 % (Window 1b)
- one 4096-point spectrum at 20 fps: 0.13 %
- each listener: about 1.2 % (WFM 2.1 %)

So ten listeners plus the spectrum stay under about 15 % of one core. The internal FFT stays.

`make dsp-deps` on the Optiplex also passed with `--expect-fftw-installed --expect-debian-liquid`.
There, Debian's FFTW-linked liquid-dsp 1.7.0 and FFTW are actually installed (`libliquid-dev`
1.7.0-1; why it is installed was not checked), and the worker still uses only the
project-built 1.8.3.
