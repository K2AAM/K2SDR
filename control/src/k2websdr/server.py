"""Run the K2AAM WebSDR control service.

    k2websdr-server --config /etc/k2aam-websdr/k2websdr.toml

Binds loopback only (see config.validate). uvicorn's own proxy-header
handling is disabled: client IPs are derived by the application from its
explicit trusted-proxy list.
"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .config import ConfigError, load
from .web.app import create_app


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="k2websdr-server")
    p.add_argument("--config", required=True)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load(a.config)
    except (ConfigError, OSError) as e:
        print(f"k2websdr-server: {e}", file=sys.stderr)
        return 78  # EX_CONFIG
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, proxy_headers=False, server_header=False,
                ws_max_size=1 << 20, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
