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

The worker (`worker/`) linked only the C/C++ standard libraries in M0. M1 adds the pinned liquid-dsp
(statically, below) and, for the Airspy HF+ source, libairspyhf (dynamically).

## M1 Airspy HF+ (worker source and Window 1 probe)

| Component | Version | License | Source |
| --- | --- | --- | --- |
| libairspyhf | 1.6.8 (Debian 13 `libairspyhf-dev` 1.6.8-4+b2, as on the Optiplex; CI installs the same from `debian:trixie`) | BSD-3-Clause (library sources; the package's GPL-2.0 command-line tools are not used) | https://github.com/airspy/airspyhf — linked dynamically by `ksdr-worker` (`--source airspyhf`) and `ksdr-airspyhf-probe` |
| libusb-1.0 | 1.0.28 (Debian 13 `libusb-1.0-0` 2:1.0.28-1) | LGPL-2.1-or-later | https://libusb.info — not linked by us; runtime dependency of libairspyhf (dynamic linking) |
| libudev | systemd 257.13 (Debian 13 `libudev1` 257.13-1~deb13u1) | LGPL-2.1-or-later | https://systemd.io — runtime dependency of libusb-1.0 (dynamic) |
| libcap | 2.75 (Debian 13 `libcap2` 1:2.75-10+deb13u1+b3) | BSD-3-Clause or GPL-2.0-only (dual) | https://sites.google.com/site/fullycapable — runtime dependency of libudev (dynamic) |
| pkg-config | Debian 13 system package | GPL-2.0-or-later | build-time tool only (locates libairspyhf); not linked |

`ksdr-airspyhf-probe-mock` and `ksdr-worker-airspyhf-mock` (tests) compile against the libairspyhf header
but link our own `worker/probe/mock_airspyhf.c`, not the library.

Versions above are what `debian:trixie` installed in CI on 2026-10-27 (CI reports them in every run as
the "Runtime packages" annotation; `tools/check_dsp_deps.py` records the worker's `ldd` closure).
Confirmed identical on the Optiplex (Adam, 2026-10-27, `dpkg-query -W libairspyhf1 libusb-1.0-0 libudev1 libcap2`).

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

## Browser UI (web/; M1)

Shipped in `web/dist` (bundled into the JavaScript, as listed by the build): only these three.

| Component | Version | License | Source |
| --- | --- | --- | --- |
| Svelte (runtime) | 5.57.2 | MIT | https://github.com/sveltejs/svelte |
| clsx | 2.1.1 | MIT | https://github.com/lukeed/clsx (Svelte runtime dependency) |
| esm-env | 1.2.2 | MIT | https://github.com/benmccann/esm-env (Svelte runtime dependency) |

Build and test only (not shipped). Exact versions are pinned in `web/package.json`, and the full
tree in `web/package-lock.json`:

| Component | Version | License | Source |
| --- | --- | --- | --- |
| Vite | 6.4.4 | MIT | https://vite.dev |
| @sveltejs/vite-plugin-svelte | 5.1.1 | MIT | https://github.com/sveltejs/vite-plugin-svelte |
| TypeScript | 5.9.3 | Apache-2.0 | https://www.typescriptlang.org |
| svelte-check | 4.7.6 | MIT | https://github.com/sveltejs/language-tools |
| Vitest | 3.2.7 | MIT | https://vitest.dev |
| @tsconfig/svelte | 5.0.8 | MIT | https://github.com/tsconfig/bases |
| Node.js / npm | Debian 13 system packages (`nodejs`, `npm`) | MIT / Artistic-2.0 | build time only; the service needs no Node |
