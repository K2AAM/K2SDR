# ADR 0007 — Licensing and provenance

Status: accepted (owner, 2026-10-26); license choice pending

**Decision.** Until a project license is chosen, the repository is "all rights
reserved, pending" and contains no OpenWebRX+ or PhantomSDR-Plus code. The
license is chosen before the RNNoise integration is ported (M2). Every
dependency is pinned exactly and recorded in `THIRD_PARTY.md` with its license
read from package metadata. The mode ledger was compared with upstream by
parsing (not executing or copying) the upstream registry.
