# Optiplex host inventory — 2026-10-26

Collected read-only by a Claude Code session running as `k2dev` on the
Optiplex (`OptiplexSDR`), 14:15–14:16 EDT, and relayed by Adam. Nothing was
changed on the host. This is evidence for A13 groundwork, not the A13 test
itself (that needs our service running; block 6).

## System

- `/etc/os-release`: Debian GNU/Linux 13 (trixie), VERSION_ID 13.
- `k2dev`: uid 1001, groups `k2dev`(1001), `users`(100). No sudo, not in
  `plugdev`.
- `make check` on the host: schema current, fixtures current, 191 tests passed,
  exit 0.

## OpenWebRX+

- `openwebrx.service`: active (running) since 2026-10-26 12:29:25 EDT; enabled.
- Processes: `openwebrx` (PID 497210) and `soapy_connector` (PID 497222).
- `soapy_connector` is using the Airspy HF+ at 192,000 samples/s, automatic
  gain, `-f 90000` (as reported by `systemctl status`).
- Status was identical before and after running `make sync` / `make check`:
  same start time, same PIDs. Our build and tests did not disturb OWRX.

## Listening TCP ports (`ss -ltnp` as k2dev)

- 8073: OpenWebRX+ HTTP.
- 127.0.0.1:37205 and 127.0.0.1:47247: OWRX `soapy_connector`.
- 80, 8084, 1073, 20241: listening, owner not identified (k2dev cannot see
  other users' processes; `sudo ss -ltnp` would name them).
- **8075 is free** (our default bind, `127.0.0.1:8075`).

## USB (`lsusb`)

| Bus/Dev | ID | Device |
| --- | --- | --- |
| 001/002 | 03eb:800c | Airspy HF+ (held by OWRX `soapy_connector`) |
| 001/003 | 8087:0033 | Intel AX211 Bluetooth |
| 001/001, 002/001 | 1d6b:0002 / 0003 | root hubs |

Not present at the time of the check: SDRplay RSPdx2, RX888 MkII.

## SDR device-node access

- `/dev/bus/usb/001/002` (Airspy HF+): `root:plugdev`, mode `crw-rw-r--+`.
- ACL (`getfacl`): read-write for `lightdm` (Debian seat access for the
  physical console). `k2dev` is not listed.
- Result: **k2dev can read but not write the SDR node**, so it cannot open the
  device. M0's "never open a physical SDR" is enforced by the OS.
- The other nodes are `root:root crw-rw-r--` without ACLs.
- Unexplained detail: the Airspy node's timestamp was 14:16 that day while the
  others dated from Oct 23 08:22; the device number did not change. Not
  investigated (would need `journalctl -k` / `udevadm info`).

## Implications

- M0: no host changes needed. 8075 is available.
- M1 (owner decision, needs root): give the worker write access to the chosen
  SDR node, via `plugdev` membership for the service account or a dedicated
  udev rule, only during scheduled hardware windows.
- Note for M1: any user logged in at the physical console also gets device
  access through the seat ACL.
