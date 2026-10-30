# K2AAM WebSDR — instructions for Claude sessions

Adam (K2AAM) is the project owner and final decision-maker. Claude proposes
designs and writes code; Adam approves architecture and hardware ownership.
Adam has delegated testing and merging: merge only after CI is green on the
exact commit being merged.

## Source of truth

`docs/outline/WebSDR_Project_Outline-v0.1.md` is normative. Do not expand scope,
invent features, or weaken explicit requirements unless Adam asks.

## Non-negotiables

- Receive-only. No transmit, ever.
- Integer Hz for every frequency in APIs, config, logs, bookmarks. Wire fields
  are JSON integers named `*_hz`. Unit strings exist only in
  `k2websdr.protocol.freq` on explicit input paths.
- The server owns global device state (capture frequency, rate, antenna,
  hardware gain). Explicit lease ownership for retunes and scans.
- Stream/session generation IDs; stale decoder events after a retune are dropped.
- Decoder audio is upstream of RNNoise, mute, volume and listening squelch.
- Map markers only from real location evidence; never from callsigns or ALE
  addresses alone.
- ALE 2G first; 3G is a later separate project. Scans are server-owned; closing
  a browser never cancels one.
- A mode is available only when adapter + dependencies + hardware bandwidth are
  actually supported.
- Permissions are enforced server-side on HTTP and WebSocket paths.
- No OpenWebRX+ or PhantomSDR-Plus code until a project license is chosen
  (`LICENSE` is "all rights reserved, pending"). Record every dependency in
  `THIRD_PARTY.md` with exact version and license.
- Never claim live RF validation without evidence. When hardware behaviour is
  uncertain, propose a measurement instead of guessing.

## Host rules (the Optiplex, Debian 13)

- OpenWebRX+ runs on this host and must keep running. Never stop, reconfigure
  or edit OWRX, its service, or its config.
- Milestone 0 never opens a physical SDR. The `k2dev` account is intentionally
  not in `plugdev` or any USB/SDR group; do not ask for that to change during
  M0. M1 hardware windows are scheduled by Adam, who grants and removes access.
  Since 2026-10-28 Adam keeps `k2dev` in `plugdev` (only he has access to the server). That
  permission is not a hardware window: never open the Airspy, or start a service configured
  with it, unless Adam has stopped OWRX for a window he scheduled.
- No sudo. If something needs root, write down the exact command and why, and
  ask Adam to run it.
- Read-only host inspection is fine: `/etc/os-release`, `ss -ltnp`, `lsusb`,
  `systemctl status` (read-only), `journalctl` where readable.
- Our service binds `127.0.0.1:8075` by default; OWRX uses 8073.

## Workflow

- One milestone at a time; within M0, work in the block order of the design
  note (1 protocol ✅, 2 fixtures ✅, 3 C++ replay worker ✅, 4 control core ✅,
  5 ledger + auth/admin/`/healthz` ✅, 6 coexistence tooling + ADRs ✅).
  **M0 complete (2026-10-26)**: A13 passed on the Optiplex (`docs/ops/a13-result-2026-10-26.md`).
  **M1 in progress**: design approved 2026-10-26 (`docs/design/M1-first-live-receiver.md`).
  Windows 1 and 1b ran 2026-10-26 (`docs/ops/airspyhf-measurements-2026-10-26.md`): H1–H8
  measured. Key facts: retune silently loses ~1490 samples @192k; stop/close hang after
  unplug; device settings persist across opens. Real clips in `fixtures/real/airspyhf/`.
  Antenna: W6LVP loop; an LZ1AQ loop is planned (re-run the H3 gain sweep when it replaces it).
  Approved gain defaults: AGC on, threshold low, LNA on, attenuator 0.
  DSP: liquid-dsp 1.8.3 built without FFTW (`make liquid`, `docs/ops/liquid-dsp-1.8.3.md`);
  worker DSP library `k2dsp` (spectrum, channels, own look-ahead AGC): `docs/design/M1-dsp.md`.
  Done: worker DSP, channels (control + browser stream v1, `docs/protocol/stream-v1.md`),
  Airspy source in the worker (`docs/protocol/worker-live-v1.md`, mock-tested) and in the service
  (`driver = "airspyhf"`, `receiver.set_hw_gain`, `docs/ops/airspyhf-receiver.md`), Window 2
  (2026-10-27, `docs/ops/window2-results-2026-10-27.md`: worker on the real Airspy, FT8 decodes,
  gain ~6 dB/step, unplug/replug), channel controls, the Svelte UI (`web/`, `make web`; spectrum,
  waterfall, RIG, AudioWorklet audio) and server-side recording (`recording.*`, /api/recordings).
  Window 3 (2026-10-27/28, `docs/ops/window3-results-2026-10-2{7,8}.md`): the whole service live
  over Tailscale Serve HTTPS :8443 (`docs/ops/window-3.md`), Mac + iPhone; every mode, B5, B7,
  B8 and B9 seen on hardware. B4 10-hour run (`docs/ops/b4-results-2026-10-28.md`): 0 dropped
  samples, CPU/RSS flat. Since then: phone layout fixed (Adam's iPhone, 2026-10-30), waterfall
  zoom + wheel tuning. UI checks without hardware: `docs/ops/window3/k2websdr-replay-tailnet.toml`.
  CPU split measured (`docs/ops/cpu-split-2026-10-30.md`): driver 2.7 %, spectrum ~3 %, ~6.3 %
  per 384k channel on the Optiplex (about half the cloud CPU's speed). WWV 10 MHz hum: likely the
  antenna/RF (Adam, 2026-10-30; also faint on OWRX): recheck with the LZ1AQ loop, with the H3 sweep.
  (Retunes now warn before ending an active recording: Adam, 2026-10-28.)
  M1 close-out: B2 needs real clips for LSB/CW/WFM/MW-AM (`tools/b2/make_clips.py` cuts them from
  the Window 1 recordings, no hardware) and NFM (needs a window); B10 needs a fresh
  `tools/coexist_check.py trial`. M2 hardware (Adam, 2026-10-30): an RX888 MkII is on its way and
  will replace the Airspy as the primary SDR on the Optiplex; the SDRplay is remote only (a site
  about 50 miles away), so SDRplay support means a remote receiver, not a local USB device.
  M1 branches: `m1/<topic>`.
- Each block: branch `m0/<topic>` → PR → CI green → merge. Keep PRs reviewable.
- Two Claude sessions may work on this repo: a cloud session (no LAN access)
  and a local session on the Optiplex (as `k2dev`). Check open PRs and branches
  before starting, and do not work on the same block in parallel. Host-only
  checks (A13 coexistence, later hardware) belong to the local session.
- Commit messages end with a `Co-Authored-By` trailer for the model used.

## Commands

```bash
make sync            # pinned env from control/uv.lock (needs uv)
make check           # schema-check + fixtures-check + all tests
make schema          # after changing k2websdr.protocol.models
make fixtures        # after changing fixtures/gen/make_synthetic.py
make ledger          # regenerate ledger/modes.v1.json from the outline appendix
make run-dev         # service on 127.0.0.1:8075 with replay receivers (docs/ops/first-run.md)
make liquid          # fetch + verify + build pinned liquid-dsp 1.8.3 (no FFTW) into build/third_party
make dsp-deps        # prove no FFTW / only the pinned liquid-dsp in the worker
make dsp-bench       # DSP benchmark on this machine
```

Build prerequisites: `build-essential`, `cmake`, `ninja-build`, `git`, `python3` (3.13), `uv`.
`make worker` builds `build/worker/ksdr-worker` (and liquid-dsp first if missing; the first
build fetches it from GitHub); `make test` builds it first. Never link Debian's `libliquid`
(it pulls in FFTW, GPL): only the project-built archive, checked by `make dsp-deps`.

## Milestone 0 acceptance (A1–A14)

A1 integer-Hz wire ✅ · A2 command lifecycle ✅ · A3 serialized queue ✅ · A4 lease
and job survival ✅ · A5 single device owner (flock) ✅ · A6 deterministic replay ✅ ·
A7 stale-generation drop ✅ · A8 fixtures + replay flags ✅ ·
A9 ledger ✅ · A10 first run, no default password ✅ · A11 HTTP+WS authz ✅ ·
A12 trusted proxies ✅ · A13 OWRX coexistence ✅ · A14 schema ✅
