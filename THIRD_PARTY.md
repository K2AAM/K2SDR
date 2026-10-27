# Third-party components

Every reused component is recorded here with its exact version, license and
source. Pins live in `control/uv.lock` (Python) and, later, in the worker's
build files. No OpenWebRX+ or PhantomSDR-Plus code is included in this
repository at this time.

## Runtime (control service)

| Component | Version | License | Source |
| --- | --- | --- | --- |
| pydantic | 2.14.1 | MIT | https://github.com/pydantic/pydantic |
| pydantic-core | 2.50.1 | MIT | https://github.com/pydantic/pydantic-core |
| annotated-types | 0.8.0 | MIT | https://github.com/annotated-types/annotated-types |
| typing-extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |
| typing-inspection | 0.4.4 | MIT | https://github.com/pydantic/typing-inspection |
| fastapi | 0.162.0 | MIT | https://github.com/fastapi/fastapi |
| starlette | 1.10.0 | BSD-3-Clause | https://github.com/encode/starlette |
| uvicorn | 0.54.0 | BSD-3-Clause | https://github.com/encode/uvicorn |
| websockets | 17.3 | BSD-3-Clause | https://github.com/python-websockets/websockets |
| h11 | 0.16.0 | MIT | https://github.com/python-hyper/h11 |
| anyio | 4.15.1 | MIT | https://github.com/agronholm/anyio (also used directly: WebSocket task groups) |
| idna | 3.20 | BSD-3-Clause | https://github.com/kjd/idna |
| click | 8.5.0 | BSD-3-Clause | https://github.com/pallets/click (uvicorn CLI dependency) |
| annotated-doc | 0.0.5 | MIT | https://github.com/fastapi/annotated-doc |
| opentelemetry-api | 1.45.1 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python (required by fastapi; API package only, no SDK or exporter is installed or configured, so nothing is sent anywhere) |
| python-multipart | 0.0.32 | Apache-2.0 | https://github.com/Kludex/python-multipart (HTML form parsing) |
| argon2-cffi | 25.1.0 | MIT | https://github.com/hynek/argon2-cffi (argon2id password hashing) |
| argon2-cffi-bindings | 26.1.0 | MIT | https://github.com/hynek/argon2-cffi-bindings |
| cffi | 2.1.1 | MIT-0 | https://github.com/python-cffi/cffi |
| pycparser | 3.12 | BSD-3-Clause | https://github.com/eliben/pycparser |

## Development and build only (not shipped)

| Component | Version | License | Source |
| --- | --- | --- | --- |
| pytest | 9.1.1 | MIT | https://github.com/pytest-dev/pytest |
| httpx2 | 2.16.0 | BSD-3-Clause | Starlette TestClient transport (tests only) |
| httpcore2 | 2.16.0 | BSD-3-Clause | httpx2 dependency (tests only) |
| truststore | 0.10.5 | MIT | httpx2 dependency (tests only) |
| httpx2-jsfetch | 1.0 | — | locked as an httpx2 platform extra; not installed on Linux |
| numpy | 2.5.4 | BSD-3-Clause | https://github.com/numpy/numpy (test-only spectral checks; fixtures are generated without it) |
| pluggy | 1.7.0 | MIT | https://github.com/pytest-dev/pluggy |
| iniconfig | 2.3.1 | MIT | https://github.com/pytest-dev/iniconfig |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | https://github.com/pypa/packaging |
| pygments | 2.21.0 | BSD-2-Clause | https://github.com/pygments/pygments |
| colorama | 0.4.6 | BSD-3-Clause | https://github.com/tartley/colorama (locked for Windows only; not installed on Linux) |
| hatchling | 1.32.4 | MIT | https://github.com/pypa/hatch |
| uv | 0.11.32 (dev host) | MIT OR Apache-2.0 | https://github.com/astral-sh/uv |
| CMake | Debian 13 system package | BSD-3-Clause | https://cmake.org (worker build) |
| Ninja | Debian 13 system package | Apache-2.0 | https://ninja-build.org (worker build) |

The worker (`worker/`) links only the C/C++ standard libraries in M0; no SDR or DSP libraries yet.

## M1 hardware measurement tools (Window 1)

| Component | Version | License | Source |
| --- | --- | --- | --- |
| libairspyhf | 1.6.8 (Debian 13 `libairspyhf-dev`/`libairspyhf1`; CI: Ubuntu 1.6.8-4build1) | BSD-3-Clause (library sources; the package's GPL-2.0 command-line tools are not used) | https://github.com/airspy/airspyhf — linked only by `ksdr-airspyhf-probe` |
| pkg-config | Debian 13 system package | GPL-2.0-or-later | build-time tool only (locates libairspyhf); not linked |

`ksdr-airspyhf-probe-mock` (tests) compiles against the libairspyhf header but links our own `worker/probe/mock_airspyhf.c`, not the library.

## DSP (worker; M1)

| Component | Version | License | Source |
| --- | --- | --- | --- |
| liquid-dsp | **1.8.3**, commit `10041f70cebbe3b97887e75bb41e48b73dda1b23` (tag v1.8.3, 2026-09-26) | MIT (notice kept in `third_party/liquid-dsp/LICENSE` and installed with the build) | https://github.com/jgaeddert/liquid-dsp — fetched and verified by `tools/fetch_liquid.py` (commit id + SHA-256 content manifest `0ce25baae2ab399a3b00f6cb12db5e4e8ac508ed0e5738435aa65e1e24df87f4`), built by `tools/build_liquid.sh`, linked **statically** into `ksdr-worker` and `ksdr-dsp-check` |

Build options (recorded per build in `build/third_party/liquid-dsp/k2-build-info.json`):
`BUILD_SHARED_LIBS=OFF BUILD_STATIC_LIBS=ON BUILD_EXAMPLES=OFF BUILD_AUTOTESTS=OFF
BUILD_BENCHMARKS=OFF BUILD_SANDBOX=OFF BUILD_DOC=OFF FIND_FFTW=OFF ENABLE_TIMESTAMPS=OFF
ENABLE_COLOR=OFF CMAKE_POSITION_INDEPENDENT_CODE=ON CMAKE_C_FLAGS=-DLIQUID_FFTOVERRIDE=1`,
Release, project-local prefix `build/third_party/liquid-dsp`. SIMD extensions are detected
on the build host (liquid's CMake runs test programs), so the archive is built for the machine
that builds it.

**FFTW is not used.** liquid-dsp's internal FFT is used instead. Debian 13's own
`libliquid1` 1.7.0 depends on `libfftw3-single3` (FFTW, GPL-2.0-or-later); it is never
linked: there is no `find_package`/`find_library` path to it, the worker includes the pinned
header (compile-time version check `worker/src/liquid_pin.hpp`), and `tools/check_dsp_deps.py`
(run by `make check` and CI, with FFTW and Debian's liquid-dsp installed on purpose) verifies
the archive's symbols, both binaries' dynamic dependencies, symbol tables and linker maps.

## Specifications referenced (not code)

| Item | Use |
| --- | --- |
| SigMF v1.2.6 | IQ fixture format; `k2websdr` extension in `fixtures/k2websdr.sigmf-ext.md` — https://sigmf.org/ |
| RFC 6902 (JSON Patch) | Subset used for state patches |
| JSON Schema 2020-12 | Generated protocol schema |
Licenses above verified against installed package metadata (License-Expression/License fields), 2026-10-26.
The HTML pages ship no third-party JavaScript or CSS.
