# ADR 0003 — IPC and data plane

Status: accepted (owner, 2026-10-26)

**Decision.** Two Unix stream sockets per worker. Control: fixed little-endian
binary messages (`protocol/worker_ctl_v1.h`), no JSON in the worker. Data:
80-byte `frame_v1` headers followed by samples in their native format (real
ADC data stays real). Headers carry `capture_gen`, a never-resetting `seq`, a
per-generation sample index, discontinuity/gap flags and the frequency origin.
The C headers are canonical; Python mirrors are byte-checked against them.

**Rejected.** gRPC (no benefit inside one host), ZeroMQ (extra dependency),
shared-memory rings (premature; candidate after M2 RX888 throughput
measurement).
