# ADR 0001 — Process topology

Status: accepted (owner, 2026-10-26)

**Decision.** One native C++ acquisition worker process per physical SDR; a
Python control service (FastAPI + pydantic v2) that owns all global state,
authentication, leases and supervision; decoders as separate supervised
processes; a Svelte/TypeScript browser client from M1.

**Why.** Real-time I/O and DSP stay out of Python request handlers (outline
§11). One worker per device isolates a USB fault to one receiver. Decoders
out of process mean a broken decoder cannot take down the service (§10).

**Rejected.** One worker for all devices (shared fate). Python acquisition
(latency, GIL). In-process decoders (fault isolation). Svelte in M0 (no UI
yet; minimal server-rendered pages instead).
