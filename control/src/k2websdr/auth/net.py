"""Client IP derivation and simple rate limiting.

Client IP (outline §9): the TCP peer, unless the peer is an explicitly
trusted proxy. Then X-Forwarded-For is walked from the right, skipping
trusted proxies, and the first untrusted address is the client. Forwarded
headers from untrusted peers are ignored entirely: they are not identity.
"""

from __future__ import annotations

import ipaddress
import time
from collections import defaultdict, deque


class ProxyPolicy:
    def __init__(self, trusted_cidrs: tuple[str, ...]) -> None:
        self.nets = [ipaddress.ip_network(c, strict=False) for c in trusted_cidrs]

    def trusted(self, addr: str | None) -> bool:
        try:
            ip = ipaddress.ip_address(addr) if addr else None
        except ValueError:
            return False
        return ip is not None and any(ip in n for n in self.nets)

    def client_ip(self, peer: str | None, x_forwarded_for: str | None) -> str | None:
        if not self.trusted(peer) or not x_forwarded_for:
            return peer
        hops = [h.strip() for h in x_forwarded_for.split(",") if h.strip()]
        for hop in reversed(hops):
            try:
                ipaddress.ip_address(hop)
            except ValueError:
                return peer        # malformed chain: fall back to the trusted proxy itself
            if not self.trusted(hop):
                return hop
        return hops[0] if hops else peer


class SlidingWindow:
    """At most `limit` events per `window` seconds per key."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit, self.window = limit, window
        self.events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        q = self.events[key]
        while q and q[0] <= now - self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


class TokenBucket:
    """rate tokens/second, up to `burst`; one token per message."""

    def __init__(self, rate: float, burst: int) -> None:
        self.rate, self.burst = rate, float(burst)
        self.tokens, self.last = float(burst), time.monotonic()

    def take(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False
