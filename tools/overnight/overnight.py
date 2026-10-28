#!/usr/bin/env python3
"""Overnight run (M1 acceptance B4): record and listen for hours, sampling the evidence.

    control/.venv/bin/python tools/overnight/overnight.py --user NAME --hours 10 \\
        --center-hz 10000000 --frequency-hz 10000000 --mode am

Signs in (password from $K2_PASSWORD, else prompted) and moves the receiver to --center-hz (with
the lease; omitted: the receiver stays where it is). It then starts a server-side recording and
opens a listening channel on the same frequency, like a browser would. It stops the recording at
the end, or on Ctrl+C. The recording is server-owned: if this script dies, it keeps going.

Every --interval seconds it appends one JSON line to <out>/samples.jsonl:
- CPU % and RSS of k2websdr-server and ksdr-worker (from /proc; the worker's pid changes if it
  restarts);
- disk free and recording storage;
- the receiver's acquisition state and capture_gen;
- the recording's seconds and gaps;
- what the listener received: audio seconds against wall-clock seconds, sample-index gaps,
  DISCONTINUITY/SERVER_DROP flags, the longest silence between audio messages, and reconnects.

<out>/summary.json holds the totals. Read-only towards the host: it opens no device and touches
nothing outside the service's API and /proc.
"""

from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import json
import os
import shutil
import signal
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "control/src"))
from k2websdr.protocol import stream as sv  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402
from websockets.sync.client import connect  # noqa: E402

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CLK = os.sysconf("SC_CLK_TCK")


def ulid() -> str:
    """A command id in the ULID alphabet: 48-bit ms timestamp + 80 random bits."""
    n = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_ALPHABET[(n >> (5 * i)) & 31] for i in range(25, -1, -1))


# ---------------------------------------------------------------------------------- the service

class Service:
    def __init__(self, base: str, user: str, password: str) -> None:
        self.base = base.rstrip("/")
        self.origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(self.base))
        self.jar = http.cookiejar.CookieJar()
        self.http = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar),
                                                urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        form = urllib.parse.urlencode({"username": user, "password": password}).encode()
        self.http.open(urllib.request.Request(self.base + "/login", data=form, headers={"Origin": self.origin}))
        if not any(c.name == "k2_session" for c in self.jar):
            raise SystemExit("sign-in failed: wrong username or password?")

    def get(self, path: str):
        with self.http.open(self.base + path, timeout=10) as r:
            return json.loads(r.read())

    def ws(self):
        cookie = "; ".join(f"{c.name}={c.value}" for c in self.jar)
        url = self.base.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
        return connect(url, additional_headers={"Cookie": cookie}, origin=self.origin, max_size=1 << 22,
                       open_timeout=10)


class Link:
    """One /ws connection: commands with results, and the audio of our channel."""

    def __init__(self, svc: Service, stats: dict) -> None:
        self.ws = svc.ws()
        self.stats = stats
        self.channel: str | None = None
        self.next_index: int | None = None
        self.gen = -1
        self.last_audio = time.monotonic()
        self.command("session.hello", client="k2websdr-overnight")

    def command(self, op: str, **args) -> dict:
        cid = ulid()
        self.ws.send(json.dumps({"v": 1, "type": "cmd", "id": cid, "op": op, "args": args}))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            msg = self.ws.recv(timeout=max(0.1, deadline - time.monotonic()))
            if isinstance(msg, bytes):
                self.on_binary(msg)
                continue
            m = json.loads(msg)
            if m.get("id") == cid and (m.get("type") == "result" or m.get("status") == "rejected"):
                return m
        raise TimeoutError(f"{op}: no result in 30 s")

    def pump(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            try:
                msg = self.ws.recv(timeout=min(left, 1.0))
            except TimeoutError:
                continue
            if isinstance(msg, bytes):
                self.on_binary(msg)

    def on_binary(self, raw: bytes) -> None:
        m = sv.unpack(raw)
        if m.kind is not sv.Kind.AUDIO or m.id != self.channel or m.gen < self.gen:
            return
        st, now = self.stats, time.monotonic()
        st["max_silence_s"] = max(st["max_silence_s"], round(now - self.last_audio, 3))
        self.last_audio = now
        if m.gen > self.gen or m.flags & sv.Flags.NEW_GEN:
            self.gen, self.next_index = m.gen, None
        if self.next_index is not None and m.sample_index != self.next_index:
            st["index_gaps"] += 1
            st["index_gap_samples"] += abs(m.sample_index - self.next_index)
        self.next_index = m.sample_index + m.count
        st["audio_messages"] += 1
        st["audio_samples"] += m.count
        st["discontinuity_flags"] += bool(m.flags & sv.Flags.DISCONTINUITY)
        st["server_drop_flags"] += bool(m.flags & sv.Flags.SERVER_DROP)


def ok(res: dict, what: str) -> dict:
    if res.get("status") != "applied":
        raise SystemExit(f"{what} refused: {res.get('code')}: {res.get('detail')}")
    return res


# -------------------------------------------------------------------------------------- /proc

def find_pids() -> dict[str, int]:
    out = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            comm = (d / "comm").read_text().strip()
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if comm.startswith("ksdr-worker"):
            out["worker"] = int(d.name)
        elif "k2websdr-server" in cmd and "overnight" not in cmd:
            out["server"] = int(d.name)
    return out


def proc_sample(pid: int) -> tuple[float, int] | None:
    """(CPU seconds so far, RSS bytes)."""
    try:
        f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        rss = next(int(x.split()[1]) * 1024 for x in Path(f"/proc/{pid}/status").read_text().splitlines()
                   if x.startswith("VmRSS:"))
    except (OSError, StopIteration):
        return None
    return (int(f[11]) + int(f[12])) / CLK, rss


# --------------------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="https://optiplexsdr.polecat-godzilla.ts.net:8443")
    ap.add_argument("--user", required=True)
    ap.add_argument("--receiver", default="hfplus")
    ap.add_argument("--center-hz", type=int, help="move the receiver here first (needs the lease)")
    ap.add_argument("--frequency-hz", type=int, required=True)
    ap.add_argument("--mode", default="am")
    ap.add_argument("--hours", type=float, default=10.0)
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--no-record", action="store_true", help="listen only")
    ap.add_argument("--state-dir", type=Path, default=Path.home() / ".k2websdr-w3/state",
                    help="for disk-free sampling")
    ap.add_argument("--out", type=Path, default=Path.home() / "w3/overnight")
    a = ap.parse_args()
    password = os.environ.get("K2_PASSWORD") or getpass.getpass(f"password for {a.user}: ")

    a.out.mkdir(parents=True, exist_ok=True)
    samples = (a.out / "samples.jsonl").open("a")
    stopping = []
    signal.signal(signal.SIGINT, lambda *_: stopping.append(1))
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))

    svc = Service(a.base, a.user, password)
    stats = {"audio_messages": 0, "audio_samples": 0, "index_gaps": 0, "index_gap_samples": 0,
             "discontinuity_flags": 0, "server_drop_flags": 0, "max_silence_s": 0.0, "reconnects": 0,
             "worker_pids": []}
    link = Link(svc, stats)
    if a.center_hz is not None:
        ok(link.command("receiver.claim", receiver=a.receiver), "receiver.claim")
        ok(link.command("receiver.set_capture", receiver=a.receiver, center_hz=a.center_hz), "receiver.set_capture")
        ok(link.command("receiver.release", receiver=a.receiver), "receiver.release")
    rec_id = None
    if not a.no_record:
        rec_id = ok(link.command("recording.start", receiver=a.receiver, frequency_hz=a.frequency_hz, mode=a.mode),
                    "recording.start")["recording"]

    def open_channel(lk: Link) -> None:
        res = ok(lk.command("channel.open", receiver=a.receiver, frequency_hz=a.frequency_hz, mode=a.mode),
                 "channel.open")
        lk.channel, lk.gen, lk.next_index = res["channel"], res.get("channel_gen") or -1, None
        lk.last_audio = time.monotonic()

    open_channel(link)
    print(f"running for {a.hours:g} h; samples in {a.out}/samples.jsonl; Ctrl+C stops early", flush=True)

    t0 = time.monotonic()
    started_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    prev_cpu: dict[str, tuple[float, float]] = {}
    end = t0 + a.hours * 3600
    while not stopping and time.monotonic() < end:
        try:
            link.pump(min(a.interval, end - time.monotonic()))
        except (ConnectionClosed, OSError, TimeoutError) as e:
            stats["reconnects"] += 1
            print(f"connection lost ({e}); reconnecting", flush=True)
            time.sleep(5)
            try:
                link = Link(svc, stats)
                open_channel(link)
            except (ConnectionClosed, OSError, TimeoutError, SystemExit) as e2:
                print(f"reconnect failed: {e2}", flush=True)
            continue
        now = time.monotonic()
        row: dict = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "elapsed_s": round(now - t0)}
        for name, pid in find_pids().items():
            ps = proc_sample(pid)
            if ps is None:
                continue
            cpu, rss = ps
            last = prev_cpu.get(f"{name}:{pid}")
            row[name] = {"pid": pid, "rss_mb": round(rss / 2**20, 1),
                         "cpu_pct": round(100 * (cpu - last[0]) / (now - last[1]), 1) if last else None}
            prev_cpu[f"{name}:{pid}"] = (cpu, now)
            if name == "worker" and pid not in stats["worker_pids"]:
                stats["worker_pids"].append(pid)
        try:
            du = shutil.disk_usage(a.state_dir)
            row["disk_free_gb"] = round(du.free / 1e9, 2)
        except OSError:
            pass
        try:
            state = svc.get("/api/state")["state"]
            rx = state["receivers"][a.receiver]
            row["acquisition"] = rx["acquisition"]
            row["capture_gen"] = rx.get("capture_gen")
            row["storage"] = state.get("storage")
            if rec_id:
                row["recording"] = state.get("recordings", {}).get(rec_id, "ended")
        except (OSError, ValueError, KeyError) as e:
            row["state_error"] = str(e)
        row["listen"] = {k: v for k, v in stats.items() if k != "worker_pids"}
        row["listen"]["audio_s"] = round(stats["audio_samples"] / 12000, 1)
        samples.write(json.dumps(row) + "\n")
        samples.flush()
        print(f"{row['utc']} {row.get('acquisition', {}).get('state')} audio {row['listen']['audio_s']} s "
              f"of {row['elapsed_s']} s, gaps {stats['index_gaps']}", flush=True)

    if rec_id:
        try:
            print("stopping the recording:", link.command("recording.stop", recording=rec_id).get("status"))
        except (ConnectionClosed, OSError, TimeoutError) as e:
            print(f"could not stop the recording ({e}); stop it in the UI", flush=True)
    wall = time.monotonic() - t0
    summary = {"started_utc": started_utc, "wall_s": round(wall), "recording": rec_id, **stats,
               "audio_s": round(stats["audio_samples"] / 12000, 1),
               "audio_ratio": round(stats["audio_samples"] / 12000 / wall, 5) if wall else None}
    (a.out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
