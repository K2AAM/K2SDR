# Window 3 results — morning of 2026-10-28

The service ran on the Airspy HF+ from 08:00 to 08:17 local time, with the new command and stream
logging (PR #40). Bundle: `k2-window3-20261028-121920.tar.gz`. Earlier sessions:
`window3-results-2026-10-27.md`.

## Evidence from the log

- **Retunes across bands work from both browsers.** The receiver moved between 7.1 MHz, 3.885
  MHz, 1.23 MHz (local AM 1230 kHz), 810 kHz, 14.074 MHz, 15 MHz, 10 MHz and 100.3 MHz (WFM),
  driven from the Mac and from the iPhone (Safari, iOS 18.7).
  - Every retune closed the channels left outside the window, as designed.
  - The page then opened a new channel by itself (PR #39).
  - The `E_OUT_OF_RANGE` rejections just before each retune are the page asking first; they are
    what makes the "Retune receiver" button appear.
- **No iPhone audio dropped by the server.** Three iPhone sessions (177 s, 59 s, 2 s) sent 16 545,
  5 526 and 183 audio messages with 0 dropped for backpressure. The Mac's 870 s session dropped
  only 1 spectrum row. Adam reports both solid today; last night's iPhone lag left no evidence
  (the logging was not there yet).
- **Recordings:** 7 more, in AM, LSB and WFM, including the 1230 kHz local station.
- **B7, iPhone lock:** audio needed a tap on "Tap to start audio" after unlocking. iOS suspends
  web audio while the phone is locked and allows resuming only from a tap, so this is the
  expected path.

## B8: unplug with the service

At 08:15, Adam pulled the USB cable with the Mac listening on WFM.

- The receiver restarted. The first four attempts after replug got `E_BUSY` ("device listed but
  could not be opened"), then it opened. The device was enumerated but could not be opened yet
  for about 7 s; device permissions probably arrive after enumeration. The retry loop handled it.
- The waterfall came back within seconds, but the audio did not, and no "start audio" button
  appeared. A reload fixed it.
- **Cause:**
  1. The new worker numbers channels from 1 again, so the reopened channel's published
     `channel_gen` went from 2 back to 1.
  2. The browser drops audio of an older generation (rule B6), so it dropped all of it.
  3. Reproduced with the mock unplug test (`assert 1 > 2`).
- **Fixed in `m1/gen-after-restart`:** the service maps the worker's channel_gen to a published
  one that never goes back.

## Still open

- **iPhone layout:** a few controls are cut off at the right edge in Safari (screenshot of
  2026-10-27). WebKit is not available in the cloud test browser.
- **A 10 kHz centre is accepted** (`freq_range_hz` lower bound). The capture span then starts
  below 0 Hz. Harmless, but the range should probably start at half the sample rate.
- **B4:** the overnight run.

## B8 re-check after PR #42 (09:15 local)

Adam pulled the USB cable with the Mac listening, waited 10 s and plugged it back in. Audio came
back by itself after about 10–15 s, with no page reload. **B8 passes on hardware.**

The B4 run (`overnight-b4.md`) started right after, at about 09:18 local, for 10 hours.
