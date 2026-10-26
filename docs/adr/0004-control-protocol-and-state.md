# ADR 0004 — Control protocol and state model

Status: accepted (owner, 2026-10-26)

**Decision.** Browser ↔ control is JSON over one WebSocket, versioned (`v: 1`).
All frequencies are JSON integers in `*_hz` fields; unit strings exist only
in the explicit input parser. Commands carry ULIDs and get exactly one `ack`
and, if accepted, one `result`. State is a revisioned snapshot plus
consecutive JSON-Patch deltas, re-validated on every commit. Hardware fields
record requested, applied (worker readback only), status and verification
separately, so an unacknowledged value never looks applied. Retries with the
same id are idempotent.

**Generated artifact.** `protocol/schema/control-v1.json`, checked in CI.
