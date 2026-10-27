"""Server configuration (TOML).

Exposure policy (owner decision, 2026-10-26): the service binds loopback only
until admin MFA exists. Any other bind address is refused at startup, not
merely warned about. Remote access in the meantime goes through an SSH or
Tailscale tunnel to the loopback port, decided outside this application.
"""

from __future__ import annotations

import ipaddress
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .protocol import models as m


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ReceiverEntry:
    id: str
    label: str
    recording: Path                      # .sigmf-meta (M0: replay receivers only)
    takeover_policy: m.TakeoverPolicy = "deny"


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 8075
    state_dir: Path = Path("/var/lib/k2aam-websdr")
    runtime_dir: Path = Path("/run/k2aam-websdr")
    worker_bin: Path = Path("/usr/lib/k2aam-websdr/ksdr-worker")
    ledger: Path = Path("/usr/share/k2aam-websdr/ledger/modes.v1.json")
    build: str = "dev"
    allowed_hosts: tuple[str, ...] = ()      # Host header values accepted (DNS-rebinding guard)
    allowed_origins: tuple[str, ...] = ()    # Origin values accepted for POST and WebSocket
    trusted_proxies: tuple[str, ...] = ()    # CIDRs whose X-Forwarded-For is believed
    cookie_secure: bool = False              # true once served over HTTPS
    session_hours: int = 12
    login_attempts_per_minute: int = 5
    ws_messages_per_second: float = 20.0
    ws_burst: int = 40
    ws_session_recheck_s: float = 30.0       # idle sockets re-check their session at least this often
    spectrum_fft: int = 2048                 # spectrum/waterfall bins per receiver (power of two)
    spectrum_fps: int = 15                   # spectrum rows per second
    receivers: tuple[ReceiverEntry, ...] = field(default_factory=tuple)

    @property
    def db_path(self) -> Path:
        return self.state_dir / "k2websdr.sqlite3"

    @property
    def setup_token_path(self) -> Path:
        return self.state_dir / "setup-token"

    @property
    def lock_dir(self) -> Path:
        return self.runtime_dir / "lock"

    @property
    def exposure(self) -> str:
        return "loopback"


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate(cfg: Config) -> Config:
    if not _is_loopback(cfg.host):
        raise ConfigError(
            f"refusing to bind {cfg.host!r}: only loopback is allowed until admin MFA is implemented "
            "(use an SSH or Tailscale tunnel to 127.0.0.1 for remote access)")
    if not 1 <= cfg.port <= 65535:
        raise ConfigError(f"bad port {cfg.port}")
    for cidr in cfg.trusted_proxies:
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            raise ConfigError(f"bad trusted_proxies entry {cidr!r}") from None
    for o in cfg.allowed_origins:
        if not (o.startswith("http://") or o.startswith("https://")) or o.endswith("/"):
            raise ConfigError(f"allowed_origins entries look like 'http://127.0.0.1:8075', got {o!r}")
    if not 0.05 <= cfg.ws_session_recheck_s <= 3600:
        raise ConfigError("ws_session_recheck_s out of range")
    if not (64 <= cfg.spectrum_fft <= 65536 and cfg.spectrum_fft & (cfg.spectrum_fft - 1) == 0):
        raise ConfigError("spectrum_fft must be a power of two, 64..65536")
    if not 1 <= cfg.spectrum_fps <= 100:
        raise ConfigError("spectrum_fps must be 1..100")
    if not 1 <= cfg.session_hours <= 24 * 30:
        raise ConfigError("session_hours out of range")
    ids = [r.id for r in cfg.receivers]
    if len(ids) != len(set(ids)):
        raise ConfigError("duplicate receiver ids")
    return cfg


def with_defaults(cfg: Config) -> Config:
    """Fill Host/Origin allow-lists from the bind address when not given explicitly."""
    from dataclasses import replace

    hosts = cfg.allowed_hosts or (f"127.0.0.1:{cfg.port}", f"localhost:{cfg.port}", "127.0.0.1", "localhost")
    origins = cfg.allowed_origins or (f"http://127.0.0.1:{cfg.port}", f"http://localhost:{cfg.port}")
    return replace(cfg, allowed_hosts=tuple(hosts), allowed_origins=tuple(origins))


def load(path: str | Path) -> Config:
    path = Path(path)
    raw = tomllib.loads(path.read_text("utf-8"))
    base = path.parent
    srv = dict(raw.get("server", {}))
    unknown = set(srv) - set(Config.__dataclass_fields__) - {"receivers"}
    if unknown:
        raise ConfigError(f"unknown [server] keys: {sorted(unknown)}")
    for key in ("state_dir", "runtime_dir", "worker_bin", "ledger"):
        if key in srv:
            p = Path(srv[key])
            srv[key] = p if p.is_absolute() else (base / p).resolve()
    for key in ("allowed_hosts", "allowed_origins", "trusted_proxies"):
        if key in srv:
            srv[key] = tuple(srv[key])
    recs = []
    for r in raw.get("receivers", []):
        rp = Path(r["recording"])
        recs.append(ReceiverEntry(id=r["id"], label=r.get("label", r["id"]),
                                  recording=rp if rp.is_absolute() else (base / rp).resolve(),
                                  takeover_policy=r.get("takeover_policy", "deny")))
    extra = set(raw) - {"server", "receivers"}
    if extra:
        raise ConfigError(f"unknown top-level tables: {sorted(extra)}")
    return validate(with_defaults(Config(**srv, receivers=tuple(recs))))
