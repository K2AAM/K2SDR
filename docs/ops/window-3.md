# Window 3 — the whole service live (Airspy HF+, Mac and iPhone over Tailscale)

About 60–90 minutes, interactive. The service runs on the real Airspy HF+ with the browser UI.
You use it from the Mac and the iPhone at `https://optiplexsdr.polecat-godzilla.ts.net:8443/`.
This window covers:

| Check | What |
|---|---|
| B3 | Every mode, on the Mac and the iPhone: AM, USB, LSB, CW, NFM, WFM |
| B5 | The same frequency in the RIG, the recording's filename and the Recordings list |
| B7 | iPhone: lock the phone, unlock, resume |
| B8 | Unplug and replug the Airspy while the service runs |
| B9 | Server-side recording that continues after the browser closes |
| B10 | OpenWebRX before and after the window |

The overnight run (B4) is a later window.

**How the browsers get in.** The service listens on 127.0.0.1:8075 only. `tailscale serve`
publishes it to your tailnet over HTTPS, on port 8443. Nothing reaches the LAN or the internet,
and OpenWebRX on 8073 is untouched. HTTPS is required anyway: browsers allow the audio player
(AudioWorklet) only on secure pages. The service config is
[`window3/k2websdr-window3.toml`](window3/k2websdr-window3.toml), checked by the real config
loader in CI.

Three things nobody has seen work yet. The first checks (step 4) settle them:

1. Your tailnet issues HTTPS certificates.
2. `tailscale serve` keeps the browser's `Host` header (the page's security policy is built from
   it).
3. `tailscale serve` passes WebSockets through.

## Terminals

SSH to the Optiplex as `k2aam` three times:

- **A**: `k2aam`, with sudo.
- **B**: `sudo -iu k2dev`. Runs the service.
- **C**: `sudo -iu k2dev`. Checks and notes.

## 1. Before (Terminal C, OpenWebRX still running)

```
cd ~/K2SDR
git fetch origin && git checkout main && git pull
make sync
make worker
make web
mkdir -p ~/w3
cp docs/ops/window3/k2websdr-window3.toml ~/k2websdr-window3.toml
python3 tools/coexist_check.py snapshot > ~/w3/owrx-before.json
```

`make web` downloads the pinned npm packages the first time. On the Mac, check that OpenWebRX
plays audio, so "after" has something to compare against. Paste anything red to Claude.

## 2. Hand over the Airspy and publish the port (Terminal A)

```
sudo usermod -aG plugdev k2dev
sudo systemctl stop openwebrx
sudo tailscale serve --bg --https=8443 127.0.0.1:8075
tailscale serve status
```

The status should show `https://optiplexsdr.polecat-godzilla.ts.net:8443` proxying to
`http://127.0.0.1:8075`. If `tailscale serve` says HTTPS certificates are not enabled, enable
them in the Tailscale admin console (DNS → HTTPS Certificates) and run the `serve` line again.

## 3. Start the service (Terminal B)

Log k2dev out and in, so the plugdev group applies:

```
exit
sudo -iu k2dev
cd ~/K2SDR
control/.venv/bin/k2websdr-server --config ~/k2websdr-window3.toml 2>&1 | tee ~/w3/service.log
```

Leave it running. Look for `first-run setup pending` and the receiver `hfplus` reaching
`running`.

## 4. First checks (Terminal C)

```
curl -sS https://optiplexsdr.polecat-godzilla.ts.net:8443/healthz; echo
curl -sS -o /dev/null -D - https://optiplexsdr.polecat-godzilla.ts.net:8443/healthz | grep -i content-security-policy
```

- **First line: JSON.** That settles question 1 (the certificate) and shows the service is
  reached. The very first HTTPS request can take 10–30 s while Tailscale fetches the certificate.
- **Second line contains `wss://optiplexsdr.polecat-godzilla.ts.net:8443`.** That settles
  question 2 (the `Host` header is kept).
  - If it shows `wss://127.0.0.1:8075` instead, stop and paste it to Claude. The browser
    would refuse the WebSocket.

Question 3, the WebSocket, is settled in step 5. The UI header says `connected` only when
`/ws` works through the proxy.

## 5. First login (Mac)

1. Read the token: `cat ~/.k2websdr-w3/state/setup-token` (Terminal C).
2. On the Mac, open `https://optiplexsdr.polecat-godzilla.ts.net:8443/setup`.
3. Enter the token, a username and a password. Then sign in.
4. You land on `/app/`. The header should show `connected`, and the receiver should show
   `running` with a moving waterfall.

If it never says `connected`, Terminal B shows the `/ws` request. Paste those lines and the
Safari console (Develop → Show JavaScript Console) to Claude.

## 6. The checks

Write what you see into `~/w3/notes.md` (Terminal C, `nano ~/w3/notes.md`), one line per check.
Include the time, so it can be matched with `service.log`. Rough notes are fine.

### B3: every mode, on the Mac, then the iPhone

Tap **Tap to start audio** first. For each line: tune, set the mode, and listen for about
20 seconds. Also note the S-meter and any crackle or gaps. The buffer line under the RIG shows
underruns; note the count.

| Mode | Where | Expect |
|---|---|---|
| AM | WWV 10 000 kHz, then 15 000 kHz | Voice and ticks (10 MHz was faint in Window 2) |
| USB | 7074 kHz | FT8 warble |
| LSB | 7150–7300 kHz | Phone, if anyone is on |
| CW | 7000–7040 kHz (or 14 000–14 060 kHz) | Morse |
| WFM | A strong local FM station, e.g. 100 100 kHz | Music or voice |
| NFM | Your NOAA weather station, 162 400–162 550 kHz | Voice |

- **Outside the window.** Frequencies outside the 384 kHz the receiver currently captures show
  a **Retune receiver to …** button. Use it; it moves the receiver for everyone.
- **WFM and NFM are the first time on VHF.** Windows 1 and 2 were HF only. If VHF does not
  work, note it and move on.
- **The HF+ has no coverage at 31–60 MHz.** Don't tune there.
- **Then the iPhone.** Open the same address on the iPhone in Safari and sign in with the same
  account. Repeat at least AM, USB and WFM. Check that the controls are usable on the small
  screen.

### B7: iPhone suspend and resume

With iPhone audio playing on WWV or FM:

1. Lock the phone for 30 s, then unlock it and return to Safari. Note what the page shows,
   whether audio comes back on its own or after a tap, and how long it takes.
2. Switch to another app for 2 minutes, then come back. Note the same.

### B9 and B5: recording

On the Mac, at 7074 kHz USB:

1. Press **● Record**. Note the frequency shown in the RIG.
2. After about 20 s, **close the browser tab** (the recording should go on without it).
3. Wait about 40 s and open the page again. The RIG should still show the recording; stop it.
4. Open **Recordings**. Note the row and download the file. In the notes, check that the
   frequency is the same in the RIG, in the row, and in the file name (`…_7074000_usb.wav`).
5. Optionally, record WFM for 20 s and note the same.

### B8: unplug with the service running

With the Mac listening to WWV or FT8:

1. Pull the Airspy's USB cable. Note the receiver's state in the UI and how long it takes to
   change. Expect `failed` with "retrying in N s"; the page itself stays up.
2. Wait about 30 s, plug it back in, and note how long until audio and the waterfall return.
   No page reload should be needed.
3. Note whether the receiver came back on the same frequency.

### Optional: worker CPU split (about 2 minutes)

This answers the open Window 2 question.

1. Stop the service first: Ctrl+C in Terminal B.
2. Run in Terminal B:
   ```
   control/.venv/bin/python tools/window2/live_check.py --serial 3B52978026BA46DA --only-cpu
   ```
3. Copy its bundle into `~/w3`.

## 7. Restore (Terminal A, always, even after a failure)

1. Stop the service with Ctrl+C in Terminal B.
2. In Terminal A:
   ```
   sudo tailscale serve --https=8443 off
   sudo systemctl start openwebrx
   sudo gpasswd -d k2dev plugdev
   tailscale serve status
   ```
   The last line should say `No serve config`. Never use `tailscale serve reset`: it would also
   remove anything else you serve.
3. On the Mac, check that OpenWebRX audio and the waterfall work again.

## 8. After (Terminal C)

```
python3 tools/coexist_check.py snapshot > ~/w3/owrx-after.json
cp ~/.k2websdr-w3/state/recordings/*.wav ~/w3/ 2>/dev/null
tar -C ~ -czf /tmp/k2-window3-$(date -u +%Y%m%d-%H%M%S).tar.gz w3
ls -lh /tmp/k2-window3-*.tar.gz
```

On the Mac: `scp k2aam@192.168.1.17:/tmp/k2-window3-*.tar.gz ~/Downloads/`, then attach it in
the chat.

The bundle holds:

- `service.log`
- `notes.md`
- the OpenWebRX snapshots
- the recordings

It holds no database, password hash or token. The window's state stays in
`~/.k2websdr-w3/` for the overnight run.
