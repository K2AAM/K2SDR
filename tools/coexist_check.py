#!/usr/bin/env python3
"""OpenWebRX+ coexistence check and A13 trial. Read-only; no sudo; stdlib only.

    python3 tools/coexist_check.py snapshot            # print a host snapshot (JSON)
    python3 tools/coexist_check.py trial               # full A13 trial (starts and stops our service)

The trial:
  1. snapshot OWRX (systemd state, start time, PIDs of openwebrx and
     soapy_connector, HTTP on :8073), listening ports and SDR USB devices;
  2. start our service (`make run-dev` equivalent) on 127.0.0.1:8075 with
     replay receivers and a throwaway state directory;
  3. while it runs: confirm it listens on loopback only, that none of our
     processes has any USB device node open, and that OWRX is unchanged;
  4. stop our service, confirm every worker exited, snapshot OWRX again and
     compare with step 1.

What it cannot check: whether OWRX *audio* stayed continuous. That needs a
person listening during the trial (or an OWRX client), and is reported as a
manual item.

It never stops, restarts or reconfigures anything except the service it
started itself.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# USB vendor:product ids of SDRs in scope. RX888 ids are the Cypress FX3 ids its
# firmware commonly uses; confirm against the real device when it arrives.
SDR_USB_IDS = {
    "03eb:800c": "Airspy HF+ Discovery",
    "1df7:*": "SDRplay (any model)",
    "04b4:00f3": "Cypress FX3 bootloader (RX888 before firmware load; unverified)",
    "04b4:00f1": "Cypress FX3 streamer (RX888 after firmware load; unverified)",
}
OWRX_PORT, OUR_PORT = 8073, 8075


# ----------------------------------------------------------------- parsing

def parse_proc_net_tcp(text: str, v6: bool) -> list[tuple[str, int]]:
    """(local address, port) for each LISTEN (state 0A) socket in /proc/net/tcp or tcp6."""
    import ipaddress
    import struct

    out = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4 or parts[3] != "0A":
            continue
        addr_hex, port_hex = parts[1].split(":")
        raw = bytes.fromhex(addr_hex)
        # The kernel prints each 32-bit word in host (little-endian) order.
        words = b"".join(struct.pack("<I", struct.unpack(">I", raw[i:i + 4])[0]) for i in range(0, len(raw), 4))
        addr = ipaddress.IPv6Address(words) if v6 else ipaddress.IPv4Address(words)
        if v6 and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        out.append((str(addr), int(port_hex, 16)))
    return out


def listening_sockets() -> list[tuple[str, int]]:
    out = []
    for path, v6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
        try:
            out += parse_proc_net_tcp(Path(path).read_text(), v6)
        except OSError:
            pass
    return sorted(set(out))


def usb_devices(sysfs: Path = Path("/sys/bus/usb/devices")) -> list[dict]:
    """USB devices from sysfs (no lsusb needed)."""
    out = []
    for d in sorted(sysfs.glob("*")):
        try:
            vid = (d / "idVendor").read_text().strip()
            pid = (d / "idProduct").read_text().strip()
            bus = int((d / "busnum").read_text())
            dev = int((d / "devnum").read_text())
        except (OSError, ValueError):
            continue
        try:
            desc = (d / "product").read_text().strip()
        except OSError:
            desc = ""
        key = f"{vid}:{pid}"
        out.append({"bus": f"{bus:03d}", "dev": f"{dev:03d}", "id": key, "desc": desc,
                    "sdr": SDR_USB_IDS.get(key) or SDR_USB_IDS.get(f"{vid}:*"),
                    "node": f"/dev/bus/usb/{bus:03d}/{dev:03d}"})
    return out


def parse_systemctl_show(text: str) -> dict:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def compare_owrx(before: dict, after: dict) -> list[str]:
    """Differences that would mean OWRX was disturbed. Empty list = unchanged."""
    problems = []
    for key in ("active_state", "active_since", "main_pid"):
        if before["owrx"].get(key) != after["owrx"].get(key):
            problems.append(f"OWRX {key} changed: {before['owrx'].get(key)!r} -> {after['owrx'].get(key)!r}")
    b = {(p["comm"], p["pid"], p["start"]) for p in before["owrx_processes"]}
    a = {(p["comm"], p["pid"], p["start"]) for p in after["owrx_processes"]}
    if b != a:
        problems.append(f"OWRX processes changed: before {sorted(b)} after {sorted(a)}")
    if before["owrx_http"] != after["owrx_http"]:
        problems.append(f"OWRX HTTP on :{OWRX_PORT} changed: {before['owrx_http']} -> {after['owrx_http']}")
    bs = sorted(d["id"] for d in before["usb"] if d["sdr"])
    as_ = sorted(d["id"] for d in after["usb"] if d["sdr"])
    if bs != as_:
        problems.append(f"SDR USB devices changed: {bs} -> {as_}")
    return problems


# --------------------------------------------------------------- host reads

def _run(argv: list[str]) -> str:
    if shutil.which(argv[0]) is None:
        return ""
    return subprocess.run(argv, capture_output=True, text=True, timeout=15).stdout


def _http_status(url: str) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError):
        return None


def _processes(names: set[str]) -> list[dict]:
    out = []
    for line in _run(["ps", "-eo", "pid=,lstart=,comm="]).splitlines():
        mt = re.match(r"\s*(\d+)\s+(\w{3} \w{3}\s+\d+ [\d:]+ \d{4})\s+(\S+)", line)
        if mt and mt.group(3) in names:
            out.append({"pid": int(mt.group(1)), "start": mt.group(2), "comm": mt.group(3)})
    return sorted(out, key=lambda p: (p["comm"], p["pid"]))


def _usb_fds_of(pid: int) -> list[str]:
    """USB device nodes a process has open. Readable only for our own processes."""
    fds = []
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if target.startswith("/dev/bus/usb/") or target.startswith("/dev/sdrplay"):
                fds.append(target)
    except (PermissionError, FileNotFoundError):
        return ["<unreadable>"]
    return fds


def snapshot() -> dict:
    osr = {}
    try:
        osr = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
    except OSError:
        pass
    show = parse_systemctl_show(_run(["systemctl", "show", "openwebrx", "-p",
                                      "ActiveState,ActiveEnterTimestamp,MainPID,NRestarts"]))
    usb = usb_devices()
    for d in usb:
        d["writable_by_me"] = os.access(d["node"], os.W_OK)
    return {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "user": os.environ.get("USER") or str(os.getuid()),
        "os": osr.get("PRETTY_NAME", "").strip('"'),
        "owrx": {"active_state": show.get("ActiveState"), "active_since": show.get("ActiveEnterTimestamp"),
                 "main_pid": show.get("MainPID"), "restarts": show.get("NRestarts")},
        "owrx_processes": _processes({"openwebrx", "soapy_connector"}),
        "owrx_http": _http_status(f"http://127.0.0.1:{OWRX_PORT}/"),
        "listening": listening_sockets(),
        "usb": usb,
        "our_processes": _processes({"ksdr-worker", "k2websdr-server"}),
    }


# ------------------------------------------------------------------- trial

def trial() -> int:
    report: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        report.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{': ' + detail if detail else ''}", flush=True)

    before = snapshot()
    print(json.dumps({"before": before}, indent=1))
    check("socket table readable", bool(before["listening"]), f"{len(before['listening'])} listening sockets")
    check("OWRX running before trial", before["owrx"]["active_state"] == "active", str(before["owrx"]))
    check(f"port {OUR_PORT} free before trial", all(p != OUR_PORT for _, p in before["listening"]))
    sdrs = [d for d in before["usb"] if d["sdr"]]
    check("this account cannot write any SDR device node", not any(d["writable_by_me"] for d in sdrs),
          ", ".join(f"{d['sdr']} {d['node']} writable={d['writable_by_me']}" for d in sdrs) or "no SDRs found")

    worker = ROOT / "build/worker/ksdr-worker"
    venv_server = ROOT / "control/.venv/bin/k2websdr-server"
    if not worker.exists() or not venv_server.exists():
        check("build present", False, "run: make sync && make worker")
        return 1

    state = Path(tempfile.mkdtemp(prefix="k2-a13-"))
    cfg = state / "a13.toml"
    cfg.write_text(f"""[server]
host = "127.0.0.1"
port = {OUR_PORT}
state_dir = "{state}/state"
runtime_dir = "{state}/run"
worker_bin = "{worker}"
ledger = "{ROOT}/ledger/modes.v1.json"
build = "a13-trial"

[[receivers]]
id = "replay-two-tone"
label = "A13 replay"
recording = "{ROOT}/fixtures/iq/two_tone_ci16.sigmf-meta"
""")
    log = open(state / "server.log", "w")
    srv = subprocess.Popen([str(venv_server), "--config", str(cfg)], stdout=log, stderr=subprocess.STDOUT,
                           start_new_session=True)
    try:
        status = None
        for _ in range(100):
            status = _http_status(f"http://127.0.0.1:{OUR_PORT}/healthz")
            if status == 200:
                break
            time.sleep(0.1)
        check("our service healthy", status == 200, f"/healthz -> {status}")
        time.sleep(3.0)                                  # let replay workers stream for a few seconds
        during = snapshot()
        ours = [(h, p) for h, p in during["listening"] if p == OUR_PORT]
        check("our service listens on loopback only", bool(ours) and all(h in ("127.0.0.1", "::1") for h, _ in ours),
              str(ours))
        workers = [p for p in during["our_processes"] if p["comm"] == "ksdr-worker"]
        check("our replay worker is running", len(workers) == 1, str(workers))
        usb_open = {p["pid"]: _usb_fds_of(p["pid"]) for p in during["our_processes"]}
        check("none of our processes has a USB device open", not any(usb_open.values()), str(usb_open))
        problems = compare_owrx(before, during)
        check("OWRX unchanged while our service runs", not problems, "; ".join(problems))
    finally:
        os.killpg(srv.pid, signal.SIGINT)
        try:
            code = srv.wait(15)
        except subprocess.TimeoutExpired:
            os.killpg(srv.pid, signal.SIGKILL)
            code = srv.wait(5)
        log.close()
    check("our service stopped cleanly", code == 0, f"exit {code}")
    time.sleep(0.5)
    after = snapshot()
    leftovers = [p for p in after["our_processes"]]
    check("no worker or server left running", not leftovers, str(leftovers))
    check(f"port {OUR_PORT} free after trial", all(p != OUR_PORT for _, p in after["listening"]))
    problems = compare_owrx(before, after)
    check("OWRX identical before and after (state, start time, PIDs, HTTP, SDR devices)", not problems,
          "; ".join(problems))
    print(json.dumps({"after": after}, indent=1))
    print("\nMANUAL: if you were listening to OWRX during the trial, report whether audio stayed continuous.")
    failed = [n for n, ok, _ in report if not ok]
    print(f"\nA13 automated checks: {len(report) - len(failed)}/{len(report)} passed"
          + (f"; FAILED: {failed}" if failed else ""))
    shutil.rmtree(state, ignore_errors=True)
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ("snapshot", "trial"):
        print(__doc__, file=sys.stderr)
        return 64
    if argv[1] == "snapshot":
        print(json.dumps(snapshot(), indent=1))
        return 0
    return trial()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
