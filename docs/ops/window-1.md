# Window 1 — Airspy HF+ measurements and recordings

Date: Tue 2026-10-27. About 45 minutes. Plan: `docs/design/M1-first-live-receiver.md` §2.

During the window the Airspy is taken away from OpenWebRX, measured, used to
record real signals, and handed back. A script does the work and tells you when
to act. You need **two terminal windows on the Optiplex**:

- **Terminal A** — your normal login (`k2aam`), for the few `sudo` commands.
- **Terminal B** — the `k2dev` account, where the script runs.

---

## Before the window (any time, ~5 min, OpenWebRX keeps running)

**Terminal A:**

```
sudo apt install -y libairspyhf-dev pkg-config
```

This is the Airspy library the measurement tool is built against (read-only
package install; OpenWebRX is not touched).

**Terminal B:**

```
sudo -iu k2dev
cd ~/K2SDR
git pull
make sync
make probe
```

`make probe` ends with a line like `Linking CXX executable ksdr-airspyhf-probe`.
If anything errors here, stop and paste it to Claude.

---

## Starting the window

**Terminal A** — let `k2dev` use USB devices for the window:

```
sudo usermod -aG plugdev k2dev
```

**Terminal B** — log `k2dev` out and back in so the change applies, then start:

```
exit
sudo -iu k2dev
cd ~/K2SDR
bash tools/window1/run_window1.sh
```

From here the script prints what it is doing and stops with a yellow `>>` line
whenever it needs you. Press **Enter** to continue, or type a short note first
(for example `glitch at the start`) and then press Enter — notes are saved with
the results.

### What it will ask you, in order

1. **"Ready: OWRX audio playing and you are listening"** — open OpenWebRX in a
   browser with audio playing. The script then tries to open the Airspy *while
   OpenWebRX is still using it* (the optional test you approved).
2. **"Did OWRX audio glitch during that?"** — type what you heard, e.g.
   `no glitch`, then Enter.
3. **"OpenWebRX stopped"** — in **Terminal A** run
   `sudo systemctl stop openwebrx`, then press Enter in Terminal B.
4. Then about 30 minutes run unattended: device info, a 1-minute test at every
   sample rate, a 10-minute sustained test, six recordings (WWV 10 and 15 MHz,
   CHU 7.85 MHz, 40 m FT8 + SSB, AM broadcast, FM broadcast) and a retune test.
5. **Unplug test** — when a red **`UNPLUG NOW`** appears, pull the Airspy's USB
   cable. When asked **"Plug the Airspy back in"**, plug it back and press Enter.
6. **"OpenWebRX started again and audio works"** — in **Terminal A** run:

   ```
   sudo systemctl start openwebrx
   sudo gpasswd -d k2dev plugdev
   ```

   Check that OpenWebRX audio works again, then press Enter in Terminal B.

The script then analyzes the recordings (a minute or two) and prints **Done**.

---

## After the window: send the results to Claude

The script's last lines show a file name like `/tmp/k2-window1-20261027-101500.tar.gz`.
On your **Mac**, in Terminal:

```
scp k2aam@192.168.1.17:/tmp/k2-window1-*.tar.gz ~/Downloads/
```

Then attach that file in the Claude chat. It is small (a few MB): the
measurements, the analysis, three short clips of real signals, and your notes.
The full recordings (about 0.8 GB) stay on the Optiplex in
`~/K2SDR/window1-results/`.

---

## If something goes wrong

The script says so in red. There are two kinds:

- **`STOPPED: ...`** — something everything else depends on failed (for example
  the Airspy could not be opened). The script stops right there.
- **`FINISHED WITH N FAILURE(S)`** — one or more measurements failed (for example
  a recording could not be written to disk) but the rest ran. Anything that
  needed the failed part was skipped.

Either way it does **not** print `Done`, it still makes the results file, and
it shows the restore commands below. Run them, then send the results file
(steps above) anyway: it shows what went wrong.

- **Press Ctrl+C** in Terminal B at any time to stop the script.
- Then, in **Terminal A**, always restore OpenWebRX:

  ```
  sudo systemctl start openwebrx
  sudo gpasswd -d k2dev plugdev
  ```

- Paste the last screen of Terminal B to Claude. Nothing in the window changes
  OpenWebRX's configuration; starting the service is all it needs.

## What the window does not do

- It never changes OpenWebRX's configuration or files.
- It does not transmit (the Airspy HF+ is receive-only).
- `k2dev` loses USB access again at the end (the `gpasswd -d` step).
