# Window 3 results — 2026-10-27 (afternoon and evening sessions)

The whole service ran on the Airspy HF+ behind `tailscale serve --https=8443`, used from the Mac
and the iPhone. Bundle: `k2-window3-20261027-200835.tar.gz` (service log, OWRX snapshots, two
recordings). Notes below are Adam's reports in the chat.

## What worked

- **HTTPS over Tailscale Serve:** the certificate, Host header and WebSocket all passed through.
  Sign-in and the UI worked from both the Mac and the iPhone (after the Referrer-Policy fix, below).
- **40 m:** strong signal, decent audio.
- **WFM:** 100.3 MHz (the first VHF reception) played well.
- **Recording (B9):** `20261027T200032Z_7074000_usb.wav` is 99.6 s, continuous. Its level is
  -11 to -16 dBFS with a dip every 15 s (the FT8 cycle). B5 holds: the file name shows
  7 074 000 Hz, matching the RIG. `…_10000000_am.wav` is 10.8 s.
- **B10:** OpenWebRX was stopped for the window and started again at 16:08:18. The after-snapshot
  shows it active with its soapy_connector, and Adam had checked it before.

## Bugs found

1. **Setup/login refused ("cross-origin request refused").** `Referrer-Policy: no-referrer`
   makes browsers send `Origin: null` on form POSTs. Fixed in PR #38 (now `same-origin`). The
   smoke test now uses the real forms.
2. **"no channel N" and lost audio after a receiver retune.**
   - A retune that leaves a channel outside the capture closes it on the server, by design.
   - The UI kept the closed channel's id and sent `channel.tune` to it. The same happened on
     the other browser, which had not retuned.
   - An active recording on that channel ends `interrupted`, also by design.
   - Fixed in `m1/channel-lost`: the listener notices that its channel was removed and opens a
     new one; a tune that gets `E_UNKNOWN_CHANNEL` opens a new channel with the change.

## Not settled

- **NFM 162.500 MHz was silent although the signal looked strong.** The offline NFM chain is
  fine: a synthetic NFM signal at offsets 0 and 40 kHz, with every AGC setting, gives audio at
  -8 to -15 dBFS. Reaching 162.5 MHz from 100.3 MHz needs a receiver retune, so bug 2 likely
  explains it, but that is not proven. Re-test after the fix. If it is still silent, record
  NFM and WFM at the same frequency, and try 162.400, 162.475 and 162.550 MHz.
- **Not run yet:**
  - B3: LSB, CW and the full iPhone mode pass.
  - B7: iPhone lock and resume.
  - B8: service unplug.
  - B4: the overnight run.

## Evening session (01:30–01:41 UTC, after PR #39)

Bundle: `k2-window3-20261028-014133.tar.gz`. The four recordings were analysed here; Adam's
reports are from the chat.

| Recording | Length | Adam | Measured |
|---|---|---|---|
| 162 500 000 Hz NFM | 35.5 s | Sounds good, "maybe some clipping" | -17 dBFS RMS. Peaks touch -6 dBFS (the AGC target), 0 flat-topped runs, 0 clipped |
| 7 219 640 Hz AM | 34.2 s | Pretty good (Radio Martí) | -16 to -18 dBFS, no clipping |
| 7 028 180 Hz CW | 44.5 s | Very good | -14 to -19 dBFS, no clipping |
| 7 194 820 Hz LSB | 110.4 s | Good; robotic until tuned to about 7.195 MHz | -18 to -22 dBFS, no clipping |

- **NFM works after the channel fix.** This supports the explanation that the afternoon's
  silence was the closed channel.
- **No clipping in the NFM audio itself.** The look-ahead AGC holds peaks at -6 dBFS without
  flat-topping, so any clipping heard comes from the transmission or from playback. To tell
  which, play the downloaded WAV and compare it with what the browser sounded like.
- **LSB robotic sound:** a few hundred Hz mistuning at the 1 kHz step. Use a 100 Hz or 10 Hz step.

**Open, with no evidence in this log.** The service logged only HTTP access lines; commands,
their rejections and stream drops were not logged. `m1/window-logging` adds those for the next
window.

- **iPhone very laggy, audio keeps cutting out.** Unknown whether the server dropped audio
  (backpressure) or the phone fell behind.
- **The iPhone could not retune to the AM broadcast band** while the Mac was on 40 m. The
  rejection reason is unknown.
