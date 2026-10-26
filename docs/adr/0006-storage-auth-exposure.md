# ADR 0006 — Storage, authentication and exposure

Status: accepted (owner, 2026-10-26)

**Decision.** SQLite (WAL) with numbered SQL migrations. argon2id passwords;
session cookies are random 256-bit tokens stored only as SHA-256 hashes, with
per-session CSRF tokens. First run uses a single-use token from a 0600 file;
there is no default account. The service binds loopback only until admin MFA
exists (owner decision); remote access is an SSH or Tailscale tunnel. Host
and Origin allow-lists guard against DNS rebinding and cross-site requests.
Client IPs come from the TCP peer unless it is an explicitly trusted proxy.

**Deferred.** Admin MFA/passkeys (required before any non-loopback exposure),
HTTPS termination and Cloudflare Tunnel support, user-management UI.
