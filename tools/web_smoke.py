#!/usr/bin/env python3
"""Browser smoke test of the UI against a real server (development tool, not CI).

    python3 tools/web_smoke.py [--screenshot out.png]     # needs `pip install playwright` + Chromium

Starts k2websdr-server (from control/.venv) on 127.0.0.1:8095 with a temporary state dir, the
built UI (make web) and a replayed real WWV clip; completes first-run setup and signs in over
HTTP; then opens /app/ in headless Chromium and checks that it connects, shows the receiver, and
logs no console errors or CSP violations. Exit 0 on success.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8095
BASE = f"http://127.0.0.1:{PORT}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--screenshot", type=Path)
    ap.add_argument("--wait-text", action="append", default=[], help="extra text that must appear")
    ap.add_argument("--timeout", type=float, default=15.0)
    a = ap.parse_args()
    from playwright.sync_api import sync_playwright

    if not (ROOT / "web/dist/index.html").is_file():
        raise SystemExit("web/dist missing: run `make web`")
    tmp = Path(tempfile.mkdtemp(prefix="k2-web-smoke-"))
    cfg = tmp / "k2.toml"
    cfg.write_text(f'''[server]
host = "127.0.0.1"
port = {PORT}
state_dir = "{tmp}/state"
runtime_dir = "{tmp}/run"
worker_bin = "{ROOT}/build/worker/ksdr-worker"
ledger = "{ROOT}/ledger/modes.v1.json"
web_dir = "{ROOT}/web/dist"

[[receivers]]
id = "replay-wwv"
label = "Replay: WWV 10 MHz"
recording = "{ROOT}/fixtures/real/airspyhf/airspyhf_wwv10_48k.sigmf-meta"
''')
    server = subprocess.Popen([str(ROOT / "control/.venv/bin/k2websdr-server"), "--config", str(cfg)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(BASE + "/healthz", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise SystemExit("server did not start")
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        hdr = {"Origin": BASE}
        token = (tmp / "state/setup-token").read_text().strip()
        pw = "correct horse battery staple"
        for path, form in (("/setup", {"token": token, "username": "adam", "password": pw}),
                           ("/login", {"username": "adam", "password": pw})):
            req = urllib.request.Request(BASE + path, data=urllib.parse.urlencode(form).encode(), headers=hdr)
            op.open(req)
        cookie = next(c for c in jar if c.name == "k2_session")

        problems: list[str] = []
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1280, "height": 800})
            ctx.add_cookies([{"name": cookie.name, "value": cookie.value, "url": BASE}])
            page = ctx.new_page()
            page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
                    if m.type in ("error", "warning") else None)
            page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
            page.goto(BASE + "/")
            page.wait_for_url(BASE + "/app/")
            for text in ["connected", "Replay: WWV 10 MHz", "running", *a.wait_text]:
                page.locator("header, main").get_by_text(text, exact=False).filter(visible=True).first.wait_for(
                    timeout=a.timeout * 1000)
            time.sleep(1.0)
            if a.screenshot:
                page.screenshot(path=str(a.screenshot))
            browser.close()
        if problems:
            print("\n".join(problems))
            return 1
        print("web smoke: OK (connected, receiver shown, no console errors or CSP violations)")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    sys.exit(main())
