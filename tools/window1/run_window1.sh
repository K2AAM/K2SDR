#!/usr/bin/env bash
# Window 1 — Airspy HF+ measurements and IQ recordings (M1 design note §2).
# Run on the Optiplex as k2dev from ~/K2SDR:   bash tools/window1/run_window1.sh
# It pauses whenever Adam needs to do something. Every result goes to one folder.
set -uo pipefail

cd "$(dirname "$0")/../.."
ROOT=$(pwd)
PROBE="${W1_PROBE:-$ROOT/build/worker/ksdr-airspyhf-probe}"
# Test-only knobs (CI rehearsal against the mock); all unset for the real window.
RATE_S=${W1_RATE_S:-60}; LONG_S=${W1_LONG_S:-600}; REC_S=${W1_REC_S:-}; UNPLUG_STREAM_S=${W1_UNPLUG_STREAM_S:-60}
CLIP_START=${W1_CLIP_START:-5}; CLIP_LEN=${W1_CLIP_LEN:-}
PY="$ROOT/control/.venv/bin/python"
TS=$(date +%Y%m%d-%H%M%S)
OUT="$ROOT/window1-results/$TS"          # gitignored; large recordings stay here
UPLOAD="/tmp/k2-window1-$TS"              # small files Adam copies to his Mac
mkdir -p "$OUT/rec" "$UPLOAD"
LOG="$OUT/window1.jsonl"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ask()  { printf '\n\033[1;33m>> %s\033[0m\n' "$*"; read -r -p "   Press Enter when done (or type a note): " REPLY; note "$1" "$REPLY"; }
note() { printf '{"event":"operator","step":%s,"note":%s}\n' "$(jq_s "$1")" "$(jq_s "$2")" >> "$LOG"; }
jq_s() { "$PY" -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"; }
run()  { echo "   \$ $*"; "$@" | tee -a "$LOG"; return "${PIPESTATUS[0]}"; }

# ------------------------------------------------------------ preconditions
say "Preconditions"
[ -n "${W1_TEST:-}" ] || [ "$(id -un)" = "k2dev" ] || { echo "Run this as k2dev."; exit 1; }
[ -x "$PROBE" ] || { echo "Probe not built: run  make probe"; exit 1; }
[ -x "$PY" ] || { echo "Python env missing: run  make sync"; exit 1; }
FREE_GB=$(df -BG --output=avail "$ROOT" | tail -1 | tr -dc 0-9)
echo "   free disk: ${FREE_GB} GB (need >= 3)"; [ "${FREE_GB:-0}" -ge 3 ] || { echo "Not enough disk."; exit 1; }
if [ -z "${W1_TEST:-}" ] && ! id -nG | tr ' ' '\n' | grep -qx plugdev; then
  echo "   k2dev is not in plugdev yet. As k2aam run:  sudo usermod -aG plugdev k2dev"
  echo "   then log k2dev out and back in (exit, then  sudo -iu k2dev ) and start this script again."
  exit 1
fi
"$PY" "$ROOT/tools/coexist_check.py" snapshot > "$OUT/owrx-before.json" && echo "   OWRX snapshot saved"

# ------------------------------------------- phase A: busy check (OWRX running)
say "Phase A — what does 'busy' look like? (OWRX keeps running)"
echo "   Listen to OpenWebRX now. This only lists the Airspy and tries to open it once."
echo "   If audio glitches at all, say so at the next prompt."
ask "Ready: OWRX audio playing and you are listening"
run "$PROBE" busy-check
ask "Did OWRX audio glitch during that? (type: no glitch / glitch + description)"

# ------------------------------------------------- phase B: device to us
say "Phase B — hand the Airspy to this test"
echo "   As k2aam, stop OpenWebRX:   sudo systemctl stop openwebrx"
ask "OpenWebRX stopped"
if pgrep -x soapy_connector >/dev/null || pgrep -x openwebrx >/dev/null; then
  echo "   OpenWebRX processes are still running — stop it before continuing."; exit 1
fi
run "$PROBE" busy-check
SERIAL=$(grep '"open_attempt"' "$LOG" | tail -1 | "$PY" -c 'import json,sys; print(json.loads(sys.stdin.read())["serial"])')
echo "   Airspy serial: $SERIAL"

say "B1 — device information (H1, H2, H3)"
run "$PROBE" info --serial "$SERIAL"
RATES=$(grep '"samplerates"' "$LOG" | tail -1 | "$PY" -c 'import json,sys; print(" ".join(map(str, json.loads(sys.stdin.read())["rates_hz"])))')
echo "   reported rates: $RATES"

say "B2 — 60 s streaming test at every reported rate (H4)"
for r in $RATES; do run "$PROBE" stream --serial "$SERIAL" --freq 10000000 --rate "$r" --seconds "$RATE_S"; done

LONG_RATE=768000; echo " $RATES " | grep -q " 768000 " || LONG_RATE=$(echo "$RATES" | tr ' ' '\n' | sort -n | tail -1)
say "B3 — 10 minute sustained stream at $LONG_RATE S/s (H4)"
run "$PROBE" stream --serial "$SERIAL" --freq 10000000 --rate "$LONG_RATE" --seconds "$LONG_S"

say "B4 — recordings (H6, H8)"
rec() { run "$PROBE" record --serial "$SERIAL" --freq "$2" --rate "$3" --seconds "${REC_S:-$4}" --out "$OUT/rec/$1"; }
rec wwv10   10005000  192000 45     # WWV 10 MHz carrier at -5 kHz
rec wwv15   15005000  192000 45     # WWV 15 MHz carrier at -5 kHz
rec chu785   7855000  192000 30     # CHU 7.85 MHz carrier at -5 kHz
rec 40m      7120000  192000 60     # 7.024-7.216 MHz: FT8 7.074 + SSB 7.125-7.216
rec mw       1000000  912000 30     # AM broadcast ~0.55-1.45 MHz
rec fm     100100000  912000 20     # FM broadcast (antenna permitting)

say "B5 — retune settling: 4 retunes between WWV 10 and 15 MHz (H5)"
run "$PROBE" record --serial "$SERIAL" --freq 10005000 --rate 192000 --seconds 15 --out "$OUT/rec/retune" \
  --retune 15005000@3 --retune 10005000@6 --retune 15005000@9 --retune 10005000@12

say "B6 — unplug test (H7)"
echo "   A 60 s stream starts now. When you see  UNPLUG NOW , pull the Airspy's USB cable."
( sleep "${W1_UNPLUG_PROMPT_S:-15}"; printf '\n\033[1;31m   >>> UNPLUG NOW <<<\033[0m\n\n' ) &
env ${W1_UNPLUG_ENV:-} "$PROBE" stream --serial "$SERIAL" --freq 10000000 --rate 768000 --seconds "$UNPLUG_STREAM_S" | tee -a "$LOG"
echo "   exit code: ${PIPESTATUS[0]}   (4 = device loss detected)"
ask "Plug the Airspy back in"
sleep "${W1_REPLUG_WAIT_S:-3}"
run "$PROBE" busy-check
run "$PROBE" info --serial "$SERIAL"

# ------------------------------------------------- phase C: give it back
say "Phase C — give the Airspy back to OpenWebRX"
echo "   As k2aam:  sudo systemctl start openwebrx"
echo "              sudo gpasswd -d k2dev plugdev"
ask "OpenWebRX started again and audio works"
"$PY" "$ROOT/tools/coexist_check.py" snapshot > "$OUT/owrx-after.json" && echo "   OWRX snapshot saved"

# ------------------------------------------------- analysis (no device needed)
say "Analysis"
for b in "$OUT"/rec/*.sigmf-meta; do "$PY" tools/window1/finalize.py "${b%.sigmf-meta}" | tee -a "$LOG"; done
"$PY" tools/window1/analyze.py carrier "$OUT/rec/wwv10"  --expect 10000000 | tee -a "$LOG"
"$PY" tools/window1/analyze.py carrier "$OUT/rec/wwv15"  --expect 15000000 | tee -a "$LOG"
"$PY" tools/window1/analyze.py carrier "$OUT/rec/chu785" --expect 7850000  | tee -a "$LOG"
"$PY" tools/window1/analyze.py settle  "$OUT/rec/retune" --expect 10000000 --expect 15000000 | tee -a "$LOG"

say "Small clips for the repository"
"$PY" tools/window1/clip.py "$OUT/rec/wwv10" "$UPLOAD/airspyhf_wwv10_48k" --center 10001000 --rate 48000 --start-s "$CLIP_START" --seconds "${CLIP_LEN:-5}"
"$PY" tools/window1/clip.py "$OUT/rec/40m"   "$UPLOAD/airspyhf_ft8_40m_48k" --center 7075500 --rate 48000 --start-s "$CLIP_START" --seconds "${CLIP_LEN:-16}"
"$PY" tools/window1/clip.py "$OUT/rec/40m"   "$UPLOAD/airspyhf_ssb_40m_48k" --center 7190000 --rate 48000 --start-s "$CLIP_START" --seconds "${CLIP_LEN:-10}"
cp "$LOG" "$OUT/owrx-before.json" "$OUT/owrx-after.json" "$UPLOAD/"
tar -C /tmp -czf "$UPLOAD.tar.gz" "$(basename "$UPLOAD")" && chmod a+r "$UPLOAD.tar.gz"

say "Done"
echo "   Full recordings (keep, ~0.8 GB): $OUT/rec"
echo "   For Claude (small):              $UPLOAD.tar.gz"
echo "   On your Mac:  scp k2aam@192.168.1.17:$UPLOAD.tar.gz ~/Downloads/"
echo "   Then attach that file in the chat."
