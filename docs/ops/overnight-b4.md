# B4 — overnight run

The whole service runs unattended for about 10 hours on the Airspy HF+, with the Window 3 setup
(`window-3.md`: OpenWebRX stopped, k2dev in plugdev, Tailscale Serve on 8443).

`tools/overnight/overnight.py` signs in like a browser and does the following:

- Tunes the receiver to WWV 10 MHz.
- Records it on the server (AM). At 12 kS/s mono that is about 86 MB an hour, inside the
  window's 1 GiB recordings cap.
- Listens on a channel of its own.
- Every minute, appends to `~/w3/overnight/samples.jsonl`:
  - CPU and memory of the service and the worker;
  - disk free;
  - the receiver's state;
  - the recording's length and gaps;
  - how much audio the listener received against wall-clock time, gaps in the sample index,
    and the longest silence.
- At the end, writes `summary.json`.

Both the service and the script run under `nohup`, so closing SSH or the Mac does not stop them.

What B4 asks for, and where it comes from:

| B4 item | Source |
|---|---|
| Dropped samples | Recording `gaps`, listener index gaps, DISCONTINUITY flags |
| Audio underruns | Listener audio seconds vs wall seconds, longest silence |
| CPU, memory | Per-minute `/proc` samples |
| Disk | Disk free, recording storage |

The commands are in the chat message of 2026-10-28. They are the same as `window-3.md` steps 1–3
with the two `nohup` lines instead of the foreground service. In the morning, stop the script
with `pkill -INT -f overnight.py` (this stops the recording and writes the summary), then the
service, then restore as in step 7.
