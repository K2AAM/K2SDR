#!/usr/bin/env python3
"""Browser smoke test of the UI against a real server (development tool, not CI).

    python3 tools/web_smoke.py [--screenshot out.png]     # needs `pip install playwright` + Chromium

Starts k2websdr-server (from control/.venv) on 127.0.0.1:8095 with a temporary state dir, the
built UI (make web) and a replayed real WWV clip; then, in headless Chromium, completes first-run setup and signs in through
the real forms, opens /app/ and checks that it connects, shows the receiver, and
logs no console errors or CSP violations. Exit 0 on success.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8095
BASE = f"http://127.0.0.1:{PORT}"


def rig_checks(page, timeout: float, problems: list[str]) -> None:
    """Channel opens on its own; audio starts from a tap and plays without underruns; typed input is
    previewed exactly before tuning; a mode change brings that mode's default passband."""
    t = timeout * 1000
    page.get_by_text("dBFS", exact=False).first.wait_for(timeout=t)          # S-meter: audio frames arrive
    page.get_by_role("button", name="Tap to start audio").click()
    page.get_by_text("buffer", exact=False).first.wait_for(timeout=t)        # jitter buffer reporting
    entry = page.get_by_label("Frequency entry")
    entry.fill("10000.5")                                                     # bare number: default unit kHz
    page.get_by_text("→ 10.000 500 MHz").wait_for(timeout=t)
    entry.fill("10 000 kHz")
    page.locator(".hint.bad").wait_for(timeout=t)                             # spaces inside: refused, not guessed
    entry.fill("10000 kHz")
    page.get_by_role("button", name="Tune").click()
    page.locator("output.big").filter(has_text="10.000 000 MHz").wait_for(timeout=t)
    page.locator("label").filter(has_text="Mode").locator("select").select_option("usb")
    lo = page.get_by_label("Passband low Hz")                                 # (no string eval: our CSP forbids it)
    for _ in range(int(timeout * 10)):
        if lo.input_value() == "300":
            break
        time.sleep(0.1)
    else:
        problems.append(f"USB passband not shown: low edge {lo.input_value()!r}")
    time.sleep(4)
    stats = page.locator(".jit").inner_text()
    underruns = int(stats.split("underruns")[1].strip())
    if underruns > 1:
        problems.append(f"audio underruns after start: {stats}")
    print("rig:", stats)


def record_checks(page, timeout: float, problems: list[str]) -> None:
    """Record 3 s from the RIG, stop, then find it (done, downloadable) in the Recordings drawer."""
    t = timeout * 1000
    page.get_by_text("dBFS", exact=False).first.wait_for(timeout=t)          # the channel is up
    freq = page.locator("output.big").inner_text()                            # B5: the same Hz everywhere
    mode = page.locator("label").filter(has_text="Mode").locator("select").input_value().upper()
    page.get_by_role("button", name="● Record").click()
    page.get_by_role("button", name="■ Stop recording").wait_for(timeout=t)
    time.sleep(3)
    shown = page.locator(".recinfo").inner_text()
    page.get_by_role("button", name="■ Stop recording").click()
    page.get_by_role("button", name="● Record").wait_for(timeout=t)
    page.get_by_role("button", name="Recordings").click()
    row = page.locator(".drawer tbody tr").first
    row.wait_for(timeout=t)
    text = row.inner_text()
    href = row.get_by_role("link", name="Download").get_attribute("href") or ""
    hz = freq.replace(" MHz", "").replace(".", "").replace(" ", "").lstrip("0")
    if "done" not in text or mode not in text or freq not in text or not href.endswith("/file") or f"_{hz}_" not in shown:
        problems.append(f"recording row looks wrong: {text!r} {href!r}")
    print("record:", shown, "|", " ".join(text.split()))


def retune_recording_checks(page, timeout: float, problems: list[str]) -> None:
    """A retune that would end an active recording warns first; Cancel keeps it, confirming ends it."""
    t = timeout * 1000
    page.get_by_text("dBFS", exact=False).first.wait_for(timeout=t)
    page.get_by_role("button", name="● Record").click()
    page.get_by_role("button", name="■ Stop recording").wait_for(timeout=t)
    entry = page.get_by_label("Frequency entry")
    for confirm in (False, True):
        entry.fill("14074 kHz")
        page.get_by_role("button", name="Tune").click()
        page.get_by_role("button", name="Retune receiver to 14.074 000 MHz").click()
        page.get_by_text("Still recording", exact=False).wait_for(timeout=t)
        if not confirm:
            page.get_by_role("button", name="Cancel").click()
            time.sleep(1)
            if page.get_by_role("button", name="■ Stop recording").count() != 1:
                problems.append("Cancel did not keep the recording")
            if "10.0" not in page.locator("output.big").inner_text():
                problems.append("Cancel retuned anyway")
        else:
            page.get_by_role("button", name="End recording and retune").click()
            page.locator("output.big").filter(has_text="14.074 000 MHz").wait_for(timeout=t)
            page.get_by_role("button", name="● Record").wait_for(timeout=t)
    print("retune-recording: warned, Cancel kept it, confirm ended it and retuned")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--screenshot", type=Path)
    ap.add_argument("--wait-text", action="append", default=[], help="extra text that must appear")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--rig", action="store_true", help="also drive the RIG: audio, frequency entry, mode")
    ap.add_argument("--record", action="store_true", help="also record 3 s and find it in the Recordings drawer")
    ap.add_argument("--retune-recording", action="store_true",
                    help="with the mock Airspy worker: a retune that would end a recording asks first")
    ap.add_argument("--mobile", action="store_true", help="iPhone-sized viewport (390x844, DPR 3, touch)")
    ap.add_argument("--settle", type=float, default=1.0, help="seconds to let the page run before checking")
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
worker_bin = "{ROOT}/build/worker/{'ksdr-worker-airspyhf-mock' if a.retune_recording else 'ksdr-worker'}"
ledger = "{ROOT}/ledger/modes.v1.json"
web_dir = "{ROOT}/web/dist"
''' + (('''
[[receivers]]
id = "hfplus"
label = "Replay: WWV 10 MHz"
driver = "airspyhf"
serial = "A1B2C3D4E5F60718"
center_hz = 10000000
rate_hz = 384000
freq_range_hz = [10000, 30000000]
''') if a.retune_recording else f'''
[[receivers]]
id = "replay-wwv"
label = "Replay: WWV 10 MHz"
recording = "{ROOT}/fixtures/real/airspyhf/airspyhf_wwv10_48k.sigmf-meta"
'''))
    env = {**os.environ, "MOCK_SETTLE_MS": "0"}          # only the mock worker reads these
    server = subprocess.Popen([str(ROOT / "control/.venv/bin/k2websdr-server"), "--config", str(cfg)], env=env,
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
        token = (tmp / "state/setup-token").read_text().strip()
        pw = "correct horse battery staple"

        problems: list[str] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
            ctx = (browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True,
                                       has_touch=True) if a.mobile
                   else browser.new_context(viewport={"width": 1280, "height": 800}))
            page = ctx.new_page()
            page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
                    if m.type in ("error", "warning") else None)
            page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
            # Setup and sign-in through the real forms: a browser, not a script, sets the Origin header.
            page.goto(BASE + "/")
            page.wait_for_url(BASE + "/setup")
            page.fill("input[name=token]", token)
            page.fill("input[name=username]", "adam")
            page.fill("input[name=password]", pw)
            page.click("button[type=submit]")
            page.wait_for_url(BASE + "/login")
            page.fill("input[name=username]", "adam")
            page.fill("input[name=password]", pw)
            page.click("button[type=submit]")
            page.wait_for_url(BASE + "/app/")
            page.locator("select[aria-label=Receiver] option", has_text="Replay: WWV 10 MHz").wait_for(
                state="attached", timeout=a.timeout * 1000)
            for text in ["connected", "running", *a.wait_text]:
                page.locator("header, main").get_by_text(text, exact=False).filter(visible=True).first.wait_for(
                    timeout=a.timeout * 1000)
            if a.rig:
                try:
                    rig_checks(page, a.timeout, problems)
                except Exception:
                    print("\n".join(problems))
                    print("RIG at failure:", page.locator("main").inner_text().replace("\n", " | "))
                    if a.screenshot:
                        page.screenshot(path=str(a.screenshot))
                    raise
            if a.record:
                record_checks(page, a.timeout, problems)
            if a.retune_recording:
                retune_recording_checks(page, a.timeout, problems)
            time.sleep(a.settle)
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
