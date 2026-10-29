"""docs/ops/window3/k2websdr-window3.toml loads with the real loader (no typos in the window)."""

from __future__ import annotations

import shutil
from pathlib import Path

from k2websdr.config import load

ROOT = Path(__file__).resolve().parents[2]


def test_window3_config_loads(tmp_path):
    shutil.copy(ROOT / "docs/ops/window3/k2websdr-window3.toml", tmp_path / "k.toml")
    cfg = load(tmp_path / "k.toml")
    rx = cfg.receivers[0].airspyhf
    assert (cfg.host, cfg.port, cfg.cookie_secure, cfg.trusted_proxies) == ("127.0.0.1", 8075, True, ("127.0.0.1/32",))
    assert "https://optiplexsdr.polecat-godzilla.ts.net:8443" in cfg.allowed_origins
    assert (rx.serial, rx.center_hz, rx.rate_hz, rx.freq_range_hz) == (0x3B52978026BA46DA, 7_100_000, 384000,
                                                                       (10_000, 165_000_000))
    assert cfg.web_dir == (tmp_path / "K2SDR/web/dist").resolve()


def test_replay_tailnet_config_loads_and_opens_no_device(tmp_path):
    shutil.copy(ROOT / "docs/ops/window3/k2websdr-replay-tailnet.toml", tmp_path / "k.toml")
    cfg = load(tmp_path / "k.toml")
    assert cfg.cookie_secure and "https://optiplexsdr.polecat-godzilla.ts.net:8443" in cfg.allowed_origins
    assert all(r.airspyhf is None for r in cfg.receivers) and len(cfg.receivers) == 2
