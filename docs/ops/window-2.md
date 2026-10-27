# Window 2 — first live run of the M1 worker (Airspy HF+)

About 20 minutes, mostly unattended. The worker on the real Airspy, driven by
`tools/window2/live_check.py` (no browser UI yet):
1. Spectrum check.
2. WWV 10 MHz on AM.
3. Gain sweep (AGC, attenuator 0/4/8, LNA).
4. Retunes to WWV 15 MHz and 40 m.
5. 40 m FT8 on USB, LSB and CW at once.
6. NFM and WFM chains (just that they run).
7. Rate change 384k → 192k → 384k.
8. 10-minute run counting drops and CPU.
9. Your unplug and replug.
10. A normal stop.

OpenWebRX is stopped for the window and started again at the end. The script never touches
it.

## Commands

Two SSH sessions to the Optiplex as `k2aam`: **Terminal A** (sudo) and **Terminal B** (`k2dev`).

**Terminal B: get and build this branch**
```
sudo -iu k2dev
cd ~/K2SDR
git fetch origin && git checkout m1/window2 && git pull
make sync
make probe
```
The last line should be a link step. Paste anything red to Claude.

**Terminal A: hand the Airspy to the window**
```
sudo usermod -aG plugdev k2dev
sudo systemctl stop openwebrx
```

**Terminal B: log k2dev out and in (for the group), then run**
```
exit
sudo -iu k2dev
cd ~/K2SDR
control/.venv/bin/python tools/window2/live_check.py --serial 3B52978026BA46DA
```

While it runs:
- **About 16 minutes** run unattended (the long run is 10 of them).
- **Red `UNPLUG NOW`:** pull the Airspy's USB cable.
- **`Plug the Airspy back in`:** plug it back, wait 3 seconds, press Enter.
- **It ends with `Done`** (or `FINISHED WITH N FAILURE(S)`) and a bundle path.

**Terminal A: always restore, even after a failure or Ctrl+C**
```
sudo systemctl start openwebrx
sudo gpasswd -d k2dev plugdev
```
Check that OpenWebRX audio works again.

**On the Mac: send the results**
```
scp k2aam@192.168.1.17:/tmp/k2-window2-*.tar.gz ~/Downloads/
```
Attach it in the chat (about 1 MB: measurements and short WAV clips).

## What it records

`events.jsonl` (every step with UTC times), `summary.json` and 12 kS/s WAVs. It records
measurements, not verdicts: the analysis happens afterwards. That includes decoding the FT8
clip, comparing the WWV tones, and the gain steps against Window 1b's about 5 dB per
attenuator step.
