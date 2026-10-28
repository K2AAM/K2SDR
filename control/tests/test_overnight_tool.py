"""tools/overnight/overnight.py against a real service with a replay receiver (B4 tooling)."""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PORT = 8098
BASE = f"http://127.0.0.1:{PORT}"
PY = str(ROOT / "control/.venv/bin/python")


def test_overnight_tool_records_listens_and_samples(tmp_path):
    cfg = tmp_path / "k.toml"
    cfg.write_text(f'''[server]
host = "127.0.0.1"
port = {PORT}
state_dir = "{tmp_path}/state"
runtime_dir = "{tmp_path}/run"
worker_bin = "{ROOT}/build/worker/ksdr-worker"
ledger = "{ROOT}/ledger/modes.v1.json"

[[receivers]]
id = "replay-wwv"
label = "Replay: WWV 10 MHz"
recording = "{ROOT}/fixtures/real/airspyhf/airspyhf_wwv10_48k.sigmf-meta"
''')
    srv = subprocess.Popen([str(ROOT / "control/.venv/bin/k2websdr-server"), "--config", str(cfg)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(BASE + "/healthz", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        token = (tmp_path / "state/setup-token").read_text().strip()
        pw = "correct horse battery staple"
        form = urllib.parse.urlencode({"token": token, "username": "adam", "password": pw}).encode()
        urllib.request.urlopen(urllib.request.Request(BASE + "/setup", data=form, headers={"Origin": BASE}))
        out = tmp_path / "out"
        run = subprocess.run([PY, str(ROOT / "tools/overnight/overnight.py"), "--base", BASE, "--user", "adam",
                              "--receiver", "replay-wwv", "--frequency-hz", "10000000", "--mode", "am",
                              "--hours", str(12 / 3600), "--interval", "4", "--state-dir", str(tmp_path),
                              "--out", str(out)],
                             env={**os.environ, "K2_PASSWORD": pw}, capture_output=True, text=True, timeout=90)
        assert run.returncode == 0, run.stdout + run.stderr
        summary = json.loads((out / "summary.json").read_text())
        rows = [json.loads(x) for x in (out / "samples.jsonl").read_text().splitlines()]
    finally:
        srv.terminate()
        srv.wait(15)
    assert summary["recording"] and summary["reconnects"] == 0
    assert 0.8 < summary["audio_ratio"] < 1.2, summary          # real-time audio for the listener
    assert summary["server_drop_flags"] == 0 and summary["worker_pids"], summary
    assert len(rows) >= 2
    last = rows[-1]
    assert last["acquisition"]["state"] == "running" and last["worker"]["rss_mb"] > 0
    assert last["recording"]["seconds"] > 5 and last["recording"]["mode"] == "am"
    assert "stopping the recording: applied" in run.stdout
