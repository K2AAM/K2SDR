# Window R1 — measure the RX888 MkII (M2 design §3.3)

About 45 minutes, mostly unattended. PhantomSDR is stopped for the window and started again at
the end. Our script never touches it. `tools/rx888/window_r1.py` runs the pinned `rx888_stream`
as a separate program and measures:

- **R1** versions and USB link speed.
- **R2** sustained streaming at 32, 50 and 64 MS/s, 10 min each: any lost samples, CPU.
- **R3** gain and attenuator steps on the WWV 10 MHz carrier.
- **R4** dither and randomizer.
- **R5** clock accuracy against WWV 5, 10 and 15 MHz.
- **R6** VHF: NOAA 162.550 MHz and FM 100.3 MHz, with a short clip of each.
- **R8** short clips for the automated tests (WWV, 40 m FT8, the strongest MW station).
- **R7** your unplug and replug, last.

## Once, before the window (k2dev, no sudo)

**1. Rust, for building rx888_stream** (installs into k2dev's home only):
```
sudo -iu k2dev
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
. "$HOME/.cargo/env"
```

**2. Build the pinned rx888_stream:**
```
cd ~/K2SDR && git pull
make sync
make rx888-stream
```
The last line should say `built: …/rx888_stream (262ab91…)`.

**3. Check that k2dev may open the RX888.** PhantomSDR installed a udev rule for its own
account; k2dev needs access too. As k2aam:
```
cat /etc/udev/rules.d/*rx888* 2>/dev/null
lsusb -d 04b4: ; ls -l /dev/bus/usb/*/* | grep -v root
```
Paste the output to Claude: from it Claude writes the exact rule (if one is needed) for you to
add with sudo. The window script also checks this and stops with a clear message before
starting anything.

## The window

Use two SSH sessions to the Optiplex as `k2aam`: **Terminal A** (sudo) and **Terminal B**
(`k2dev`).

**Terminal A: stop PhantomSDR**
```
sudo systemctl stop phantomsdr-receiver
~/PhantomSDR-Plus/stop-websdr.sh
pgrep -a "rx888_stream|spectrumserver" || echo "PhantomSDR stopped"
```
The last line must print `PhantomSDR stopped`.

**Terminal B: run the window**
```
sudo -iu k2dev
cd ~/K2SDR
control/.venv/bin/python tools/rx888/window_r1.py
```
- **About 40 minutes unattended.** R2 alone is 30 minutes.
- **Red `UNPLUG … NOW`:** pull the RX888's USB cable and press Enter.
- **`Plug the RX888 back in`:** plug it into the same blue port, wait 3 seconds, press Enter.
- **It ends** with `Done` (or `FINISHED WITH N FAILURE(S)`) and a `Bundle:` path.
- It refuses to start, changing nothing, while any `rx888_stream` or `spectrumserver` runs.
- **Disk:** it needs about 2 GB of temporary space and leaves 256 MB of raw samples in
  `~/K2SDR/rx888-results/` for development.

**Terminal A: always restore, even after a failure or Ctrl+C**
```
sudo systemctl start phantomsdr-receiver
```
Check that PhantomSDR works again.

**On the Mac: send the results**
```
scp k2aam@192.168.1.17:/tmp/k2-r1-*.tar.gz ~/Downloads/
```
Attach it in the chat (a few MB: measurements and clips).
