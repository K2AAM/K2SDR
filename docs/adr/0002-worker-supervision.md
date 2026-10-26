# ADR 0002 — Worker supervision

Status: accepted (owner, 2026-10-26); revisit at M6

**Decision.** The control service spawns workers as child processes with two
inherited socketpairs, restarts them with bounded retries and exponential
backoff, and passes `--capture-gen-start last + 1` so a receiver's generation
never goes backwards. Workers set `PR_SET_PDEATHSIG` and exit when the
control socket closes, so a dead control service never leaves a device held.

**Rejected for now.** systemd template units per device
(`ksdr-worker@<id>`): attractive for handoff, but it splits ownership of
device state between systemd and the control service. Re-evaluated with
packaging in M6.
