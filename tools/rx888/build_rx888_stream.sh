#!/usr/bin/env bash
# Build the pinned rx888_stream (M2 backend A: a separate process, GPL-3.0, never linked into our
# code) into build/third_party/rx888_stream/. Needs cargo (Rust); no sudo:
#   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
#   . "$HOME/.cargo/env"
# Pinned: commit and the sha256 of the FX3 firmware image it ships (THIRD_PARTY.md).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REPO="https://github.com/rhgndf/rx888_stream.git"
COMMIT="262ab9166810bd6b021ff9a5868ff7739525cc7a"
FW_SHA256="824c575fd00ef0faa0c099ee013c6b8c19ec735bd1e555651c49273fa6b30ff2"
OUT="$ROOT/build/third_party/rx888_stream"
SRC="$OUT/src"

command -v cargo >/dev/null || { echo "cargo not found: install Rust for this user first (see the header of $0)"; exit 1; }
mkdir -p "$OUT"
if [ ! -d "$SRC/.git" ]; then
  git clone --quiet "$REPO" "$SRC"
fi
git -C "$SRC" fetch --quiet origin
git -C "$SRC" checkout --quiet --detach "$COMMIT"
[ "$(git -C "$SRC" rev-parse HEAD)" = "$COMMIT" ] || { echo "commit mismatch"; exit 1; }
echo "$FW_SHA256  $SRC/SDDC_FX3.img" | sha256sum --check --quiet || { echo "firmware image hash mismatch"; exit 1; }
(cd "$SRC" && cargo build --release --locked --quiet)
install -m 0755 "$SRC/target/release/rx888_stream" "$OUT/rx888_stream"
install -m 0644 "$SRC/SDDC_FX3.img" "$OUT/SDDC_FX3.img"
echo "$COMMIT" > "$OUT/COMMIT"
echo "built: $OUT/rx888_stream ($COMMIT), firmware $OUT/SDDC_FX3.img"
