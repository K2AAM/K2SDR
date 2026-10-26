# ADR 0005 — Hardware ownership

Status: accepted (owner, 2026-10-26)

**Decision.** Two layers.

1. *Between our processes:* an exclusive `flock` per device, taken by the
   worker before it opens the source and released by the kernel if it dies.
2. *Inside the service:* one lease per receiver, held by an operator session
   or a server job. Hardware commands need the lease. Takeover needs both the
   claimant's request and the receiver's `preempt_with_notice` policy, and is
   visible in state. Job leases never depend on a browser session.

**Not covered:** OpenWebRX+ does not take our lock. Sharing a device with it
is a deliberate owner-run handoff (`docs/ops/owrx-handoff.md`). In M0 the
`k2dev` account cannot write SDR device nodes, so this is enforced by the OS.
