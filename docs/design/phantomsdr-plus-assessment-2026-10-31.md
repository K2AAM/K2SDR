# PhantomSDR-Plus: fork or not? — assessment, 2026-10-31

Adam asked whether to fork PhantomSDR-Plus after trying it on the RX888 MkII ("decent, noise
filtering great; a bit cluttered, half a million scripts"). He authorised reading its source
**for this assessment only**. Nothing from it is in this repository. The rule against using its
code stands until a licence is chosen.

## What was read

- **Repository:** `github.com/sv1btl/PhantomSDR-Plus` (what Adam installed), commit `d56aad18`
  (2026-11-01, "Upgrade to v.5.4.0").
- **Files:** licence, history, layout and sizes; the core server (`src/`), the DSP path and the
  frontend layout.

## Facts

| | |
|---|---|
| Licence | **GPL-3.0 plus section 7 additional terms**. Any copy or modified version that has a UI must keep "PhantomSDR+ \| v… original from github.com/sv1btl/PhantomSDR-Plus" visible, with that link, on the receiver page and the /mobile page. It must also be marked as different from the original |
| History | 65 commits since an "Initial commit" on 2026-09-20 (earlier PhantomSDR history not carried over). One maintainer. Messages are mostly "update" or "Upgrade to v.5.4.0" (three such in two days) |
| Size (excluding subprojects) | C++ server ≈ 11 k lines in `src/` (+ 16 k in headers). Frontend JS ≈ 117 k lines plus Svelte ≈ 34 k, of which `App.svelte` alone is 22 408 lines. 55 shell scripts ≈ 30 k lines. Python ≈ 12 k lines |
| Tests | Only JS8 decoder tests. None for the server, the DSP path or the UI |
| Core design | One whole-band FFT (FFTW, or OpenCL/CUDA/MKL), shared by everyone. Waterfall rows are cut from it per client. Each listener's audio is demodulated **on the server** from a slice of FFT bins (inverse FFT with 50 % overlap-add, i.e. fast-convolution down-conversion), so an extra listener is cheap |
| Browser side | Many decoders run in the browser (CW, FSK, fax, HFDL, JS8, DRM helper, …). RNNoise noise reduction runs in the browser (`frontend/src/lib/rnnoise.js`) |
| Also in the server | Chat, WebSDR.org registration, a Kiwi client emulation bridge, markers. A geo-IP lookup of each listener's address against `http://ip-api.com` (plain HTTP, a third-party service) |
| Operations | The installer makes system-wide changes. On the Optiplex it installed a DRM helper on port 8075, purged Debian's Rust, and added Intel's Ubuntu (Jammy) apt repository to Debian 13. The admin panel defaults to password `admin` |

## Assessment

**What is genuinely good:**
- The whole-band FFT with cheap per-listener down-conversion. This is the right architecture for
  the RX888. Our M1 channel design (each channel mixes and resamples the full stream, ~6 % of an
  Optiplex core per channel at 384 kS/s) does not scale to 60 MS/s.
- Browser-side RNNoise.
- The breadth of browser decoders.

**Why a fork is a poor fit:**
1. **Little of the outline exists there.** Server-owned device state with leases, permissions
   enforced on every HTTP/WebSocket path, generation IDs against stale data, server-owned scans
   and recordings that survive the browser, integer-Hz wire fields, ALE, the decoder event
   pipeline and the map are all missing. They would have to be built into a codebase whose main
   UI file is 22 k lines and that has no tests.
2. **Upstream churn.** Releases are fast and opaque. Keeping a fork current would mean merging
   large, unexplained changes repeatedly.
3. **Licence consequences.** Forking, or reusing any of its material, makes our project GPL-3. It
   also carries the section 7 terms: their attribution must stay visible in our UI.
4. **Privacy and security defaults** we would have to undo: geo-IP lookups to a third party over
   plain HTTP, and a default admin password.

**Recommendation:** do not fork. Keep this project and take PhantomSDR-Plus's *ideas*, which are
standard DSP and design techniques and need none of its code, into M2:

- whole-band FFT for the RX888, with fast-convolution (overlap-add/overlap-save) per-channel
  down-conversion, and optional OpenCL acceleration;
- RNNoise in the browser, from upstream xiph/rnnoise (BSD-3);
- browser-side decoders where they fit the outline's decoder pipeline (M3+), from their own
  upstream sources.

The project licence is a separate decision for Adam. GPL-3 is a reasonable choice in its own
right, but it would not by itself make reusing PhantomSDR-Plus material free of the attribution
term.
