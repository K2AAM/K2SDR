# Handing an SDR from OpenWebRX+ to K2AAM WebSDR (and back)

Status: procedure for M1+ hardware test windows. **Not used in M0** (M0 opens
no physical SDR). Every step here is performed or approved by Adam; nothing in
this repository automates it, and our service never edits OWRX configuration.

## Why a handoff is needed

Only one process may open a physical SDR. Our own processes coordinate
through a per-device lock (`<runtime_dir>/lock/<device-id>.lock`), but
OpenWebRX+ does not take that lock. So the two never share a device: one
owns it for a window, deliberately.

## Before a window

1. Choose the device and the window. Tell anyone listening on OWRX.
2. Record the starting state: `python3 tools/coexist_check.py snapshot > before.json`.

## Hand the device to K2AAM WebSDR

1. In OWRX's SDR device settings, disable that SDR, or, if every OWRX
   device can pause, stop OWRX: `sudo systemctl stop openwebrx`. (Owner action.)
2. Confirm nothing holds the device: the soapy_connector for it is gone, and
   `python3 tools/coexist_check.py snapshot` lists the device.
3. Grant the worker's account write access for the window (owner action),
   e.g. `sudo usermod -aG plugdev k2dev` then log k2dev out and in, or a
   dedicated udev rule. Record what was granted.
4. Enable the device in our configuration (the per-device acquisition switch
   arrives with hardware support in M1) and start.

## Hand it back

1. Stop our service; confirm no `ksdr-worker` remains (`coexist_check.py snapshot`).
2. Remove the access granted in step 3 (`sudo gpasswd -d k2dev plugdev`, or
   remove the udev rule).
3. Re-enable the device in OWRX (or `sudo systemctl start openwebrx`).
4. Confirm OWRX audio and waterfall work again.

## Open questions to measure in M1/M2 (not guessed here)

- What each driver reports when another process already holds the device
  (libairspyhf vs. SoapyAirspyHF), so `external_busy` can be shown precisely.
- Whether the SDRplay API service lets a second client see or claim an
  RSPdx2 that OWRX is using, and whether that affects OWRX's stream.
