# liquid-dsp (third-party, MIT)

`LICENSE` here is liquid-dsp's own notice, copied verbatim from the pinned source
(`tools/fetch_liquid.py`: v1.8.3, commit `10041f70cebbe3b97887e75bb41e48b73dda1b23`). The
source itself is not vendored: `make liquid` fetches that commit, verifies it (commit id and
SHA-256 content manifest) and builds it statically without FFTW into `build/third_party/`.
See `THIRD_PARTY.md` for the build options and how FFTW exclusion is verified.
