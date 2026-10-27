#!/usr/bin/env bash
# Build the pinned liquid-dsp as a static library into a project-local prefix.
#
#   tools/build_liquid.sh [PREFIX]       (default: build/third_party/liquid-dsp)
#
# Never installs outside PREFIX and never touches Debian's libliquid or anything else on the
# host. FFTW is excluded twice: FIND_FFTW=OFF (no find_package, no -lfftw3f) and
# LIQUID_FFTOVERRIDE (the source never selects the FFTW code path). The result is checked
# afterwards by tools/check_dsp_deps.py; this script also refuses a cache that found FFTW.
# Offline: K2_LIQUID_SRC=/path/to/verified/tree uses that tree (still manifest-verified).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
PREFIX=$(realpath -m "${1:-$ROOT/build/third_party/liquid-dsp}")
SRC="$ROOT/build/third_party/src/liquid-dsp"
BLD="$ROOT/build/third_party/liquid-dsp-build"

python3 tools/fetch_liquid.py --dest "$SRC" ${K2_LIQUID_SRC:+--from "$K2_LIQUID_SRC"}

OPTS=(
  -DCMAKE_BUILD_TYPE=Release
  -DBUILD_SHARED_LIBS=OFF -DBUILD_STATIC_LIBS=ON
  -DBUILD_EXAMPLES=OFF -DBUILD_AUTOTESTS=OFF -DBUILD_BENCHMARKS=OFF -DBUILD_SANDBOX=OFF -DBUILD_DOC=OFF
  -DFIND_FFTW=OFF
  -DENABLE_TIMESTAMPS=OFF -DENABLE_COLOR=OFF
  -DCMAKE_POSITION_INDEPENDENT_CODE=ON
  "-DCMAKE_C_FLAGS=-DLIQUID_FFTOVERRIDE=1"
  -DCMAKE_INSTALL_PREFIX="$PREFIX"
)
cmake -S "$SRC" -B "$BLD" -G Ninja "${OPTS[@]}" > "$BLD.configure.log" 2>&1 || { cat "$BLD.configure.log"; exit 1; }
CACHE="$BLD/CMakeCache.txt"
grep -qx 'FIND_FFTW:BOOL=OFF' "$CACHE" || { echo "liquid-dsp: FIND_FFTW is not OFF in $CACHE"; exit 1; }
if grep -Eq '^fftw3f_(LIBRARY|INCLUDE_DIR):[A-Z]+=/' "$CACHE"; then
  echo "liquid-dsp: the CMake cache located FFTW; refusing to build"; exit 1
fi
cmake --build "$BLD"
rm -rf "$PREFIX"
cmake --install "$BLD" > /dev/null
install -D -m 0644 "$SRC/LICENSE" "$PREFIX/share/doc/liquid-dsp/LICENSE"

simd=$( (grep -E '^C_[A-Z0-9]+_FOUND:BOOL=TRUE' "$CACHE" || true) | sed -E 's/^C_([A-Z0-9]+)_FOUND.*/\1/' | sort | paste -sd, -)
python3 - "$PREFIX" "$simd" "${OPTS[@]}" <<'PY'
import json, platform, subprocess, sys
sys.path.insert(0, "tools")
import fetch_liquid as f
prefix, simd, opts = sys.argv[1], sys.argv[2], sys.argv[3:]
cc = subprocess.run(["cc", "--version"], capture_output=True, text=True).stdout.splitlines()[0]
info = {"name": f.PIN_NAME, "version": f.PIN_VERSION, "commit": f.PIN_COMMIT, "repo": f.PIN_REPO,
        "source_manifest_sha256": f.PIN_MANIFEST_SHA256, "license": "MIT",
        "cmake_options": [o for o in opts if not o.startswith("-DCMAKE_INSTALL_PREFIX")],
        "simd_detected_on_build_host": simd.split(",") if simd else [], "compiler": cc,
        "machine": platform.machine()}
open(f"{prefix}/k2-build-info.json", "w").write(json.dumps(info, indent=2) + "\n")
print(f"liquid-dsp {f.PIN_VERSION} built into {prefix} (static, no FFTW; SIMD: {simd or 'none'})")
PY
