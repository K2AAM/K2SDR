"""Ctrl+C in the service's terminal (SIGINT to its process group) stops the service, which stops
its workers; the worker is not in that group, so it does not exit on its own first (Window 3)."""

from __future__ import annotations

import os
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PORT = 8097


def _children(pid: int) -> list[int]:
    out = []
    for task in Path(f"/proc/{pid}/task").iterdir():
        kids = (task / "children").read_text().split()
        out += [int(k) for k in kids]
    return out


def test_sigint_to_the_terminal_group_does_not_reach_the_worker(tmp_path):
    cfg = tmp_path / "k.toml"
    cfg.write_text(f'''[server]
host = "127.0.0.1"
port = {PORT}
state_dir = "{tmp_path}/state"
runtime_dir = "{tmp_path}/run"
worker_bin = "{ROOT}/build/worker/ksdr-worker"
ledger = "{ROOT}/ledger/modes.v1.json"

[[receivers]]
id = "r"
label = "r"
recording = "{ROOT}/fixtures/real/airspyhf/airspyhf_wwv10_48k.sigmf-meta"
''')
    log = (tmp_path / "log").open("w")
    srv = subprocess.Popen([str(ROOT / "control/.venv/bin/k2websdr-server"), "--config", str(cfg)],
                           stdout=log, stderr=subprocess.STDOUT, start_new_session=True)   # its own "terminal" group
    try:
        for _ in range(200):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/healthz", timeout=1)
                workers = [k for k in _children(srv.pid)
                           if Path(f"/proc/{k}/comm").read_text().strip().startswith("ksdr-worker")]
                if workers:
                    break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            raise AssertionError("service or worker did not start")
        (worker,) = workers
        assert os.getpgid(worker) != os.getpgid(srv.pid)
        os.killpg(srv.pid, signal.SIGINT)                   # what the terminal does on Ctrl+C
        assert srv.wait(15) == 0
        for _ in range(100):
            if not Path(f"/proc/{worker}").exists():
                break
            time.sleep(0.1)
        else:
            raise AssertionError("worker outlived the service")
    finally:
        if srv.poll() is None:
            srv.kill()
        log.close()
    text = (tmp_path / "log").read_text()
    assert "worker exited with code" not in text and "restart failed" not in text, text
