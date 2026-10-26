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
  Next: Milestone 1 design note, for Adam's approval before any M1 code.
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
```

Build prerequisites: `build-essential`, `cmake`, `ninja-build`, `python3` (3.13), `uv`.
`make worker` builds `build/worker/ksdr-worker`; `make test` builds it first.

## Milestone 0 acceptance (A1–A14)

A1 integer-Hz wire ✅ · A2 command lifecycle ✅ · A3 serialized queue ✅ · A4 lease
and job survival ✅ · A5 single device owner (flock) ✅ · A6 deterministic replay ✅ ·
A7 stale-generation drop ✅ · A8 fixtures + replay flags ✅ ·
A9 ledger ✅ · A10 first run, no default password ✅ · A11 HTTP+WS authz ✅ ·
A12 trusted proxies ✅ · A13 OWRX coexistence ✅ · A14 schema ✅
