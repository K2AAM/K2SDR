#!/usr/bin/env bash
# Window 1 — Airspy HF+ measurements and IQ recordings (M1 design note §2).
# Run on the Optiplex as k2dev from ~/K2SDR:   bash tools/window1/run_window1.sh
# It pauses whenever Adam needs to do something. Every result goes to one folder.
#
# Failure policy: a step everything else depends on (finding/opening the Airspy,
# reading its info) stops the session at once. An independent measurement that
# fails is recorded and the session continues; whatever depends on it (analysis,
# clips of a failed recording) is skipped. Any failure means a nonzero exit, a
# "FAILED" banner instead of "Done", and the OpenWebRX restore commands on screen.
# The results bundle is still written so the failure can be diagnosed.
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

FAILURES=()
declare -A REC_OK=()
failed() { FAILURES+=("$1"); printf '   \033[1;31mFAILED: %s\033[0m\n' "$1"; note "failure" "$1"; }
restore_help() {
  printf '\n\033[1;31m   Give the Airspy back to OpenWebRX now. As k2aam run:\033[0m\n'
  echo "      sudo systemctl start openwebrx"
  echo "      sudo gpasswd -d k2dev plugdev"
}
bundle() {
  for f in "$LOG" "$OUT/owrx-before.json" "$OUT/owrx-after.json"; do [ -f "$f" ] && cp "$f" "$UPLOAD/"; done
  printf '%s\n' "${FAILURES[@]}" > "$UPLOAD/FAILURES.txt"
  tar -C /tmp -czf "$UPLOAD.tar.gz" "$(basename "$UPLOAD")" && chmod a+r "$UPLOAD.tar.gz"
}
send_help() {
  echo "   For Claude (small):  $UPLOAD.tar.gz"
  echo "   On your Mac:  scp k2aam@192.168.1.17:$UPLOAD.tar.gz ~/Downloads/"
  echo "   Then attach that file in the chat."
}
# A required step failed: nothing after it can produce valid results.
abort() {
  failed "$1"
  printf '\n\033[1;31m== STOPPED: %s\033[0m\n' "$1"
  echo "   Later steps were skipped because they depend on this one."
  restore_help
  bundle; send_help
  exit 1
}
trap 'abort "interrupted (Ctrl+C)"' INT

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
run "$PROBE" busy-check || failed "busy-check while OWRX runs (exit $?)"
ask "Did OWRX audio glitch during that? (type: no glitch / glitch + description)"

# ------------------------------------------------- phase B: device to us
say "Phase B — hand the Airspy to this test"
echo "   As k2aam, stop OpenWebRX:   sudo systemctl stop openwebrx"
ask "OpenWebRX stopped"
if pgrep -x soapy_connector >/dev/null || pgrep -x openwebrx >/dev/null; then
  abort "OpenWebRX processes are still running after the stop step"
fi
BUSY=$(run "$PROBE" busy-check) || abort "busy-check with OpenWebRX stopped (exit $?)"
echo "$BUSY"
SERIAL=$(printf '%s\n' "$BUSY" | "$PY" -c '
import json, sys
ok = [e["serial"] for e in map(json.loads, (l for l in sys.stdin if l.startswith("{")))
      if e.get("event") == "open_attempt" and e.get("opened")]
print(ok[0] if ok else "")')
[ -n "$SERIAL" ] || abort "no Airspy could be opened with OpenWebRX stopped (see the busy-check lines above)"
echo "   Airspy serial: $SERIAL"

say "B1 — device information (H1, H2, H3)"
INFO=$(run "$PROBE" info --serial "$SERIAL") || { echo "$INFO"; abort "device info (exit $?)"; }
echo "$INFO"
RATES=$(printf '%s\n' "$INFO" | grep '"samplerates"' | tail -1 | "$PY" -c 'import json,sys; print(" ".join(map(str, json.loads(sys.stdin.read())["rates_hz"])))' 2>/dev/null)
[ -n "$RATES" ] || abort "the Airspy reported no sample rates"
echo "   reported rates: $RATES"

say "B2 — 60 s streaming test at every reported rate (H4)"
for r in $RATES; do
  run "$PROBE" stream --serial "$SERIAL" --freq 10000000 --rate "$r" --seconds "$RATE_S" || failed "stream at $r S/s (exit $?)"
done

LONG_RATE=768000; echo " $RATES " | grep -q " 768000 " || LONG_RATE=$(echo "$RATES" | tr ' ' '\n' | sort -n | tail -1)
say "B3 — 10 minute sustained stream at $LONG_RATE S/s (H4)"
run "$PROBE" stream --serial "$SERIAL" --freq 10000000 --rate "$LONG_RATE" --seconds "$LONG_S" \
  || failed "sustained stream at $LONG_RATE S/s (exit $?)"

say "B4 — recordings (H6, H8)"
rec() {
  if run "$PROBE" record --serial "$SERIAL" --freq "$2" --rate "$3" --seconds "${REC_S:-$4}" --out "$OUT/rec/$1"; then
    REC_OK[$1]=1
  else
    failed "recording $1 (exit $?; 5 = disk write failed, 4 = device lost)"
  fi
}
rec wwv10   10005000  192000 45     # WWV 10 MHz carrier at -5 kHz
rec wwv15   15005000  192000 45     # WWV 15 MHz carrier at -5 kHz
rec chu785   7855000  192000 30     # CHU 7.85 MHz carrier at -5 kHz
rec 40m      7120000  192000 60     # 7.024-7.216 MHz: FT8 7.074 + SSB 7.125-7.216
rec mw       1000000  912000 30     # AM broadcast ~0.55-1.45 MHz
rec fm     100100000  912000 20     # FM broadcast (antenna permitting)

say "B5 — retune settling: 4 retunes between WWV 10 and 15 MHz (H5)"
RT=$(run "$PROBE" record --serial "$SERIAL" --freq 10005000 --rate 192000 --seconds 15 --out "$OUT/rec/retune" \
  --retune 15005000@3 --retune 10005000@6 --retune 15005000@9 --retune 10005000@12); RT_RC=$?
echo "$RT"
if [ "$RT_RC" -ne 0 ]; then
  failed "retune recording (exit $RT_RC)"
elif printf '%s\n' "$RT" | grep '"event":"retune"' | grep -qv '"result":0'; then
  failed "the Airspy rejected at least one retune (see the retune lines above)"
else
  REC_OK[retune]=1
fi

say "B6 — unplug test (H7)"
echo "   A 60 s stream starts now. When you see  UNPLUG NOW , pull the Airspy's USB cable."
( sleep "${W1_UNPLUG_PROMPT_S:-15}"; printf '\n\033[1;31m   >>> UNPLUG NOW <<<\033[0m\n\n' ) &
env ${W1_UNPLUG_ENV:-} "$PROBE" stream --serial "$SERIAL" --freq 10000000 --rate 768000 --seconds "$UNPLUG_STREAM_S" | tee -a "$LOG"
UNPLUG_RC=${PIPESTATUS[0]}
echo "   exit code: $UNPLUG_RC   (4 = device loss detected)"
[ "$UNPLUG_RC" -eq 4 ] || failed "unplug was not detected (exit $UNPLUG_RC, expected 4)"
ask "Plug the Airspy back in"
sleep "${W1_REPLUG_WAIT_S:-3}"
run "$PROBE" busy-check || failed "busy-check after replug (exit $?)"
run "$PROBE" info --serial "$SERIAL" || failed "Airspy not usable after replug (exit $?)"

# ------------------------------------------------- phase C: give it back
say "Phase C — give the Airspy back to OpenWebRX"
echo "   As k2aam:  sudo systemctl start openwebrx"
echo "              sudo gpasswd -d k2dev plugdev"
ask "OpenWebRX started again and audio works"
trap 'abort "interrupted (Ctrl+C) during analysis — OpenWebRX was already restored"' INT
"$PY" "$ROOT/tools/coexist_check.py" snapshot > "$OUT/owrx-after.json" && echo "   OWRX snapshot saved"

# ------------------------------------------------- analysis (no device needed)
say "Analysis"
# Only recordings that completed are analysed or clipped; the rest are already listed as failures.
step() {  # step NEEDED_RECORDING DESCRIPTION CMD...
  local need=$1 what=$2; shift 2
  if [ -z "${REC_OK[$need]:-}" ]; then echo "   skipped: $what (recording $need failed)"; return; fi
  "$@" | tee -a "$LOG"; local rc=${PIPESTATUS[0]}
  [ "$rc" -eq 0 ] || failed "$what (exit $rc)"
}
for name in "${!REC_OK[@]}"; do
  "$PY" tools/window1/finalize.py "$OUT/rec/$name" | tee -a "$LOG"
  [ "${PIPESTATUS[0]}" -eq 0 ] || { failed "finalize $name"; unset "REC_OK[$name]"; }
done
step wwv10  "carrier analysis wwv10"  "$PY" tools/window1/analyze.py carrier "$OUT/rec/wwv10"  --expect 10000000
step wwv15  "carrier analysis wwv15"  "$PY" tools/window1/analyze.py carrier "$OUT/rec/wwv15"  --expect 15000000
step chu785 "carrier analysis chu785" "$PY" tools/window1/analyze.py carrier "$OUT/rec/chu785" --expect 7850000
step retune "retune analysis"         "$PY" tools/window1/analyze.py retune  "$OUT/rec/retune" --expect 10000000 --expect 15000000

say "Small clips for the repository"
step wwv10 "clip wwv10"    "$PY" tools/window1/clip.py "$OUT/rec/wwv10" "$UPLOAD/airspyhf_wwv10_48k" --center 10001000 --rate 48000 --start-s "$CLIP_START" --seconds "${CLIP_LEN:-5}"
step 40m   "clip FT8 40m"  "$PY" tools/window1/clip.py "$OUT/rec/40m"   "$UPLOAD/airspyhf_ft8_40m_48k" --center 7075500 --rate 48000 --start-s "$CLIP_START" --seconds "${CLIP_LEN:-16}"
step 40m   "clip SSB 40m"  "$PY" tools/window1/clip.py "$OUT/rec/40m"   "$UPLOAD/airspyhf_ssb_40m_48k" --center 7190000 --rate 48000 --start-s "$CLIP_START" --seconds "${CLIP_LEN:-10}"
bundle

if [ "${#FAILURES[@]}" -gt 0 ]; then
  printf '\n\033[1;31m== FINISHED WITH %d FAILURE(S)\033[0m\n' "${#FAILURES[@]}"
  printf '   - %s\n' "${FAILURES[@]}"
  echo "   OpenWebRX was already handed back above. Send the bundle anyway: it shows what went wrong."
  send_help
  exit 1
fi
say "Done"
echo "   Full recordings (keep, ~0.8 GB): $OUT/rec"
send_help
