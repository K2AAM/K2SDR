#!/usr/bin/env python3
"""Window 2: the M1 worker on the real Airspy HF+ (first live run). Run by Adam on the Optiplex.

    control/.venv/bin/python tools/window2/live_check.py --serial 3B52978026BA46DA

Drives build/worker/ksdr-worker --source airspyhf through the same client the control service
uses (WorkerProcess). Mostly unattended; it stops only to ask for the unplug and the replug.
Writes events.jsonl, summary.json and short WAV clips (12 kS/s mono) to
window2-results/<timestamp>/, and a small bundle to /tmp/k2-window2-<timestamp>.tar.gz.
It never touches OpenWebRX: stopping and restarting it are Adam's steps in the runbook.

Each step records measurements, not verdicts about RF quality; the analysis happens afterwards.
`--test` runs against ksdr-worker-airspyhf-mock with short timings (CI / development only).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import subprocess
import sys
import tarfile
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "control/src"))

import numpy as np  # noqa: E402

from k2websdr.devices.worker_client import Callbacks, WorkerProcess, WorkerStartError  # noqa: E402
from k2websdr.protocol import worker_ctl as wc  # noqa: E402
from k2websdr.protocol.frame import FrameFlags, FrameKind  # noqa: E402
from k2websdr.replay.worker_argv import HwGain, WorkerIdentity, airspyhf_argv  # noqa: E402

M, G = wc.Mode, wc.Agc
RATE = 384000
FFT = 2048
YELLOW, RED, GREEN, RESET = "\033[33m", "\033[31m", "\033[32m", "\033[0m"


def say(msg: str, color: str = "") -> None:
    print(f"{color}{msg}{RESET if color else ''}", flush=True)


@dataclass
class Capture:
    """Everything one worker process sends, collected as it arrives."""
    spectrum: list = field(default_factory=list)              # (time, capture_gen, bins uint8, flags)
    audio: dict = field(default_factory=dict)                 # channel id -> list[(time, hdr, pcm int16)]
    iq_frames: int = 0
    statuses: list = field(default_factory=list)
    exit_code: int | None = None
    exit_at: float | None = None

    def on_frame(self, hdr, payload) -> None:
        t = time.monotonic()
        if hdr.kind is FrameKind.SPECTRUM:
            self.spectrum.append((t, hdr.capture_gen, np.frombuffer(payload, np.uint8).copy(), int(hdr.flags)))
        elif hdr.kind is FrameKind.AUDIO:
            self.audio.setdefault(hdr.stream_id, []).append((t, hdr, np.frombuffer(payload, "<i2").copy()))
        elif hdr.kind is FrameKind.SAMPLES:
            self.iq_frames += 1

    def on_exit(self, code: int) -> None:
        self.exit_code, self.exit_at = code, time.monotonic()


class Window2:
    def __init__(self, a: argparse.Namespace) -> None:
        self.a = a
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = Path(a.results_dir) / stamp
        self.bundle = Path(a.bundle_dir) / f"k2-window2-{stamp}.tar.gz"
        if self.dir.exists() or self.bundle.exists():
            raise SystemExit(f"{self.dir} or {self.bundle} already exists; refusing to overwrite")
        self.dir.mkdir(parents=True)
        (self.dir / "locks").mkdir()
        self.events = (self.dir / "events.jsonl").open("w")
        self.summary: dict = {"started_utc": datetime.now(timezone.utc).isoformat(), "serial": a.serial,
                              "worker": str(a.worker), "test_mode": a.test, "steps": {}}
        self.failures: list[str] = []
        self.cap: Capture | None = None
        self.wp: WorkerProcess | None = None
        self.center = 0
        self.rate = RATE

    # ------------------------------------------------------------------ plumbing
    def log(self, event: str, **kw) -> None:
        rec = {"t_utc": datetime.now(timezone.utc).isoformat(), "event": event, **kw}
        self.events.write(json.dumps(rec) + "\n")
        self.events.flush()

    def step(self, name: str, **result) -> None:
        self.summary["steps"][name] = result
        self.log("step", name=name, **result)
        say(f"   {name}: {json.dumps(result)[:300]}")

    def fail(self, name: str, why: str) -> None:
        self.failures.append(f"{name}: {why}")
        self.summary["steps"][name] = {"error": why}
        self.log("failure", name=name, error=why)
        say(f"   {name} FAILED: {why}", RED)

    def ask(self, prompt: str) -> str:
        self.log("prompt", prompt=prompt)
        if self.a.test:
            return "(test)"
        try:                                          # drop keys pressed earlier (Window 2: a stray Enter
            import termios                            # skipped the replug prompt)
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
        except (ImportError, OSError, ValueError):
            pass
        say(f">> {prompt}", YELLOW)
        note = input("   (press Enter; you can type a note first) ").strip()
        self.log("answer", prompt=prompt, note=note)
        return note

    async def start(self, center: int, rate: int = RATE, gain: HwGain = HwGain()) -> None:
        self.cap = Capture()
        ident = WorkerIdentity(device_id=f"airspyhf-{self.a.serial:016x}", lock_dir=self.dir / "locks", receiver_idx=0)
        cap = self.cap
        self.wp = WorkerProcess(
            lambda c, d, g: airspyhf_argv(self.a.worker, ident, serial=self.a.serial, rate_hz=rate, center_hz=center,
                                          ctl_fd=c, data_fd=d, gain=gain, capture_gen_start=g, iq_frames=False,
                                          spectrum_fft=FFT, spectrum_fps=10),
            Callbacks(on_frame=cap.on_frame, on_status=cap.statuses.append, on_exit=cap.on_exit),
            start_timeout=10.0)
        await self.wp.start(1)
        self.center, self.rate = center, rate
        self.log("worker_started", pid=self.wp.pid, center_hz=center, rate_hz=rate, gain=gain.__dict__ if hasattr(
            gain, "__dict__") else str(gain))

    async def open(self, cid: int, mode: wc.Mode, freq_hz: int, agc: wc.Agc = G.MEDIUM) -> wc.ChannelApplied:
        ap = await self.wp.channel_open(wc.Channel(cid, mode, agc, freq_hz - self.center), 5)
        self.log("channel_open", id=cid, mode=mode.name, freq_hz=freq_hz, channel_gen=ap.channel_gen)
        return ap

    async def close(self, cid: int) -> None:
        await self.wp.channel_close(cid, 5)

    def secs(self, s: float) -> float:
        return min(s, self.a.short) if self.a.test else s

    def spectrum_since(self, t0: float, gen: int | None = None) -> np.ndarray:
        rows = [b for t, g, b, _ in self.cap.spectrum if t >= t0 and (gen is None or g == gen)]
        return np.mean(rows, axis=0) if rows else np.zeros(FFT)

    @staticmethod
    def db(q):
        return -127.5 + 0.5 * np.asarray(q, float)

    def audio_since(self, cid: int, t0: float, gen: int | None = None):
        fr = [(h, p) for t, h, p in self.cap.audio.get(cid, []) if t >= t0
              and (gen is None or h.audio_ext.channel_gen == gen)]
        pcm = np.concatenate([p for _, p in fr]) if fr else np.zeros(0, np.int16)
        return fr, pcm

    def wav(self, name: str, pcm: np.ndarray) -> str:
        p = self.dir / f"{name}.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(12000)
            w.writeframes(pcm.astype("<i2").tobytes())
        return p.name

    @staticmethod
    def audio_stats(fr, pcm) -> dict:
        idx = [h.first_sample_index for h, _ in fr]
        contiguous = all(b == a + len(p) for (a, (_, p)), b in zip(zip(idx, fr), idx[1:]))
        disc = sum(FrameFlags.DISCONTINUITY in h.flags for h, _ in fr[1:])
        x = pcm.astype(float) / 32768
        spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) if len(x) > 1024 else np.zeros(2)
        f = np.fft.rfftfreq(len(x), 1 / 12000) if len(x) > 1024 else np.zeros(2)
        peak = float(f[int(np.argmax(spec[1:])) + 1]) if len(x) > 1024 else None
        lv = [h.audio_ext.level_cdb / 100 for h, _ in fr]
        return {"seconds": round(len(pcm) / 12000, 2), "frames": len(fr), "contiguous": contiguous,
                "discontinuities": disc, "rms_dbfs": round(20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-12), 1)
                if len(x) else None, "peak_audio_hz": peak,
                "channel_level_dbfs_median": round(float(np.median(lv)), 1) if lv else None}

    def worker_cpu(self) -> dict:
        try:
            st = Path(f"/proc/{self.wp.pid}/stat").read_text().split(")")[1].split()
            ticks = os.sysconf("SC_CLK_TCK")
            status = Path(f"/proc/{self.wp.pid}/status").read_text()
            rss = int(next(x.split()[1] for x in status.splitlines() if x.startswith("VmRSS")))
            return {"cpu_s": (int(st[11]) + int(st[12])) / ticks, "rss_kb": rss}
        except (OSError, StopIteration, IndexError):
            return {}

    # --------------------------------------------------------------------- steps
    async def s1_open_and_spectrum(self) -> None:
        await self.start(10_000_000)
        t0 = time.monotonic()
        await asyncio.sleep(self.secs(3))
        b = self.db(self.spectrum_since(t0 + 0.5))
        bin_hz = self.rate / FFT
        peak = int(np.argmax(b))
        self.step("1_open_spectrum", center_hz=10_000_000, rows=len(self.cap.spectrum),
                  peak_offset_hz=round((peak - FFT // 2) * bin_hz), peak_dbfs=round(float(b[peak]), 1),
                  median_dbfs=round(float(np.median(b)), 1), wwv_carrier_bin_dbfs=round(float(b[FFT // 2]), 1))

    async def s2_wwv_am(self, name: str, seconds: float) -> None:
        ap = await self.open(1, M.AM, self.center)
        t0 = time.monotonic()
        await asyncio.sleep(self.secs(seconds))
        fr, pcm = self.audio_since(1, t0, ap.channel_gen)
        await self.close(1)
        self.step(name, wav=self.wav(name, pcm), freq_hz=self.center, **self.audio_stats(fr, pcm))

    async def s3_gain_sweep(self) -> None:
        """Does SET_HW act on the device? Spectrum level at the WWV carrier and the noise median."""
        rows = []
        for label, hw in (("agc_on_default", wc.SetHw(True, False, True, 0)),
                          ("agc_off_att0", wc.SetHw(False, False, True, 0)),
                          ("agc_off_att4", wc.SetHw(False, False, True, 4)),
                          ("agc_off_att8", wc.SetHw(False, False, True, 8)),
                          ("agc_off_att0_lna_off", wc.SetHw(False, False, False, 0)),
                          ("agc_on_restored", wc.SetHw(True, False, True, 0))):
            t_call = time.monotonic()
            ap = await self.wp.set_hw(hw, 5)
            await asyncio.sleep(self.secs(1.0))                # settle
            t0 = time.monotonic()
            await asyncio.sleep(self.secs(3.0))
            b = self.db(self.spectrum_since(t0))
            rows.append({"setting": label, "applied": [ap.hf_agc, ap.hf_agc_threshold_high, ap.hf_lna, ap.hf_att],
                         "call_ms": round((t0 - t_call - self.secs(1.0)) * 1000, 1),
                         "carrier_dbfs": round(float(b[FFT // 2 - 2:FFT // 2 + 3].max()), 1),
                         "median_dbfs": round(float(np.median(b)), 1)})
        self.step("3_gain_sweep", rows=rows)

    async def s4_retune(self, to_hz: int) -> None:
        gen0 = self.cap.spectrum[-1][1] if self.cap.spectrum else None
        t_call = time.monotonic()
        ap = await self.wp.set_capture(to_hz, None, 5)
        t_applied = time.monotonic()
        await asyncio.sleep(self.secs(2))
        new = [(t, g) for t, g, _, _ in self.cap.spectrum if g == ap.capture_gen]
        old_after = [t for t, g, _, _ in self.cap.spectrum if g == gen0 and t > t_applied + 0.05]
        self.center = to_hz
        self.step(f"4_retune_{to_hz}", applied_gen=ap.capture_gen, applied_center_hz=ap.center_hz,
                  verified=ap.verified, call_ms=round((t_applied - t_call) * 1000, 1),
                  first_new_row_ms=round((new[0][0] - t_applied) * 1000, 1) if new else None,
                  old_gen_rows_after_applied=len(old_after))

    async def s5_ssb_cw(self) -> None:
        """40 m: FT8 on USB (decodable afterwards), LSB phone and CW at once. Content is whatever is on air."""
        await self.s4_retune(7_100_000)
        chans = [(2, M.USB, 7_074_000, "40m_ft8_usb"), (3, M.LSB, 7_200_000, "40m_lsb"), (4, M.CW, 7_030_000, "40m_cw")]
        aps = {cid: await self.open(cid, mode, f) for cid, mode, f, _ in chans}
        t0 = time.monotonic()
        start_utc = datetime.now(timezone.utc).isoformat()
        await asyncio.sleep(self.secs(45))
        for cid, mode, f, name in chans:
            fr, pcm = self.audio_since(cid, t0, aps[cid].channel_gen)
            await self.close(cid)
            self.step(name, wav=self.wav(name, pcm), freq_hz=f, mode=mode.name, start_utc=start_utc,
                      **self.audio_stats(fr, pcm))

    async def s6_fm_chains(self) -> None:
        """NFM and WFM chains on live samples (no FM signal is expected on HF: this only shows they run)."""
        await self.s4_retune(29_600_000)
        a1 = await self.open(5, M.NFM, 29_600_000)
        a2 = await self.open(6, M.WFM, 29_600_000)
        t0 = time.monotonic()
        await asyncio.sleep(self.secs(8))
        for cid, ap, name in ((5, a1, "10m_nfm_chain"), (6, a2, "10m_wfm_chain")):
            fr, pcm = self.audio_since(cid, t0, ap.channel_gen)
            await self.close(cid)
            self.step(name, wav=self.wav(name, pcm), note="chain check only, no signal expected",
                      **self.audio_stats(fr, pcm))

    async def s7_rate_change(self) -> None:
        await self.s4_retune(10_000_000)
        out = {}
        for rate in (192000, 384000):
            t_call = time.monotonic()
            ap = await self.wp.set_capture(None, rate, 10)
            out[str(rate)] = {"gen": ap.capture_gen, "rate": ap.sample_rate_hz,
                              "call_ms": round((time.monotonic() - t_call) * 1000, 1)}
            self.rate = rate
            await asyncio.sleep(self.secs(2))
        self.step("7_rate_change", **out)
        await self.s2_wwv_am("7_wwv10_am_after_rate_change", 10)

    async def s8_long_run(self) -> None:
        seconds = self.a.long_seconds if not self.a.test else self.a.short
        say(f"   8_long_run: {seconds:.0f} s at {RATE} S/s with spectrum and two channels (no input needed)")
        await self.open(7, M.AM, 10_000_000)
        await self.open(8, M.USB, 10_001_000)
        cpu0, t0, rows0 = self.worker_cpu(), time.monotonic(), len(self.cap.spectrum)
        ru0 = resource.getrusage(resource.RUSAGE_SELF)
        await asyncio.sleep(seconds)
        cpu1, t1 = self.worker_cpu(), time.monotonic()
        ru1 = resource.getrusage(resource.RUSAGE_SELF)
        rows = self.cap.spectrum[rows0:]
        fr7, pcm7 = self.audio_since(7, t0)
        fr8, _ = self.audio_since(8, t0)
        flags = [f for _, _, _, f in rows]
        self.step("8_long_run", seconds=round(t1 - t0, 1), spectrum_rows=len(rows),
                  spectrum_rows_expected=round((t1 - t0) * 10),
                  spectrum_discontinuities=sum(bool(f & FrameFlags.DISCONTINUITY) for f in flags),
                  audio_am=self.audio_stats(fr7, pcm7), audio_usb_frames=len(fr8),
                  audio_usb_discontinuities=sum(FrameFlags.DISCONTINUITY in h.flags for h, _ in fr8[1:]),
                  worker_cpu_pct=round(100 * (cpu1.get("cpu_s", 0) - cpu0.get("cpu_s", 0)) / (t1 - t0), 2)
                  if cpu0 and cpu1 else None, worker_rss_kb=cpu1.get("rss_kb"),
                  script_cpu_pct=round(100 * ((ru1.ru_utime + ru1.ru_stime) - (ru0.ru_utime + ru0.ru_stime))
                                       / (t1 - t0), 2))
        await self.close(7)
        await self.close(8)

    async def s9_unplug(self) -> None:
        say("   9_unplug: next you pull the Airspy's USB cable while it streams.")
        if not self.a.test:
            say("UNPLUG NOW: pull the Airspy's USB cable", RED)
        t0 = time.monotonic()
        while self.cap.exit_code is None and time.monotonic() - t0 < (30 if self.a.test else 120):
            await asyncio.sleep(0.05)
        st = [s for s in self.cap.statuses if s.state is wc.WorkerState.FAILED]
        if self.cap.exit_code is None:
            self.fail("9_unplug", "worker still running 120 s after the prompt (cable not pulled, or loss not seen)")
            return
        self.step("9_unplug", worker_exit_code=self.cap.exit_code, expected_exit_code=69,
                  status_failed=[s.result.name for s in st],
                  seconds_from_prompt_to_exit=round(self.cap.exit_at - t0, 2))
        self.ask("Plug the Airspy back in (or re-authorize it), wait 3 seconds")
        t_replug = time.monotonic()
        attempt = 0
        while True:                                   # like the service: keep trying, up to 60 s here
            try:
                await self.start(10_000_000)
                break
            except WorkerStartError as e:
                self.log("restart_attempt", attempt=attempt, result=e.result and e.result.name, exit=e.exit_code)
                attempt += 1
                if time.monotonic() - t_replug > (5 if self.a.test else 60):
                    self.wp = None
                    self.fail("9_replug", f"worker could not reopen the device within 60 s ({attempt} tries)")
                    return
                await asyncio.sleep(1)
        t0 = time.monotonic()
        await asyncio.sleep(self.secs(3))
        self.step("9_replug", attempts=attempt + 1, seconds_to_reopen=round(t0 - t_replug, 2),
                  spectrum_rows_3s=sum(1 for t, *_ in self.cap.spectrum if t >= t0))

    def thread_cpu(self) -> dict[int, float]:
        """CPU seconds per thread of the worker (tid -> user+system)."""
        out = {}
        tick = os.sysconf("SC_CLK_TCK")
        for t in os.listdir(f"/proc/{self.wp.pid}/task"):
            try:
                st = Path(f"/proc/{self.wp.pid}/task/{t}/stat").read_text()
                f = st[st.rindex(")") + 2:].split()
                out[int(t)] = (int(f[11]) + int(f[12])) / tick
            except (OSError, ValueError, IndexError):
                pass
        return out

    async def s11_cpu_split(self) -> None:
        """Where the worker's CPU goes (Window 2: 19 % of a core, about twice the estimate). Per thread:
        the worker's own (main) thread runs the loop, ring and DSP; the others are libairspyhf/libusb."""
        seconds = self.secs(30)
        rows = []
        configs = (("no_dsp", False, False), ("spectrum", True, False), ("spectrum_2ch", True, True))
        for label, spectrum, chans in configs:
            await self.start(10_000_000)
            if not spectrum:                        # restart without spectrum frames
                await self.wp.stop(10)
                ident = WorkerIdentity(device_id=f"airspyhf-{self.a.serial:016x}", lock_dir=self.dir / "locks",
                                       receiver_idx=0)
                self.cap = Capture()
                cap = self.cap
                self.wp = WorkerProcess(
                    lambda c, d, g: airspyhf_argv(self.a.worker, ident, serial=self.a.serial, rate_hz=RATE,
                                                  center_hz=10_000_000, ctl_fd=c, data_fd=d, capture_gen_start=g,
                                                  iq_frames=False),
                    Callbacks(on_frame=cap.on_frame, on_status=cap.statuses.append, on_exit=cap.on_exit),
                    start_timeout=10.0)
                await self.wp.start(1)
            if chans:
                await self.open(7, M.AM, 10_000_000)
                await self.open(8, M.USB, 10_001_000)
            await asyncio.sleep(self.secs(3))
            c0, t0 = self.thread_cpu(), time.monotonic()
            await asyncio.sleep(seconds)
            c1, t1 = self.thread_cpu(), time.monotonic()
            pct = {tid: round(100 * (c1[tid] - c0.get(tid, 0)) / (t1 - t0), 2) for tid in c1}
            main = pct.pop(self.wp.pid, 0.0)
            rows.append({"config": label, "worker_main_thread_pct": main,
                         "other_threads_pct": round(sum(pct.values()), 2), "other_threads": sorted(pct.values()),
                         "total_pct": round(main + sum(pct.values()), 2)})
            await self.wp.stop(10)
            self.wp = None
        self.step("11_cpu_split", rate_hz=RATE, rows=rows)

    async def s10_normal_stop(self) -> None:
        if self.wp is None or not self.wp.running:
            self.step("10_normal_stop", skipped="no worker running")
            return
        t0 = time.monotonic()
        await self.wp.stop(10)
        self.step("10_normal_stop", exit_code=self.cap.exit_code, seconds=round(time.monotonic() - t0, 2),
                  expected_exit_code=0)

    # ----------------------------------------------------------------------- run
    async def run(self) -> int:
        steps = [("1", self.s1_open_and_spectrum), ("2", lambda: self.s2_wwv_am("2_wwv10_am", 20)),
                 ("3", self.s3_gain_sweep), ("4", lambda: self.s4_retune(15_000_000)),
                 ("4b", lambda: self.s2_wwv_am("4_wwv15_am", 15)), ("5", self.s5_ssb_cw), ("6", self.s6_fm_chains),
                 ("7", self.s7_rate_change), ("8", self.s8_long_run), ("9", self.s9_unplug),
                 ("10", self.s10_normal_stop)]
        if self.a.only_cpu:
            steps = [("11", self.s11_cpu_split)]
        if self.a.only_unplug:
            steps = [("1", self.s1_open_and_spectrum), ("9", self.s9_unplug), ("10", self.s10_normal_stop)]
        say("== Window 2: first live run of the M1 worker on the Airspy HF+ ==", GREEN)
        for key, fn in steps:
            if key == "9" and self.a.skip_unplug:
                continue
            try:
                await fn()
            except WorkerStartError as e:
                self.fail(f"step_{key}", f"worker could not start: {e}")
                if key == "1":
                    break
            except Exception as e:  # noqa: BLE001 - record and carry on with what can still run
                self.fail(f"step_{key}", f"{type(e).__name__}: {e}")
                if self.wp is None or not self.wp.running:
                    if key in ("9",):
                        continue
                    break
        if self.wp is not None and self.wp.running:
            await self.wp.stop(10)
        return self.finish()

    def finish(self) -> int:
        self.summary["finished_utc"] = datetime.now(timezone.utc).isoformat()
        self.summary["failures"] = self.failures
        (self.dir / "summary.json").write_text(json.dumps(self.summary, indent=2))
        self.events.close()
        with tarfile.open(self.bundle, "w:gz") as tar:
            for p in sorted(self.dir.iterdir()):
                if p.is_file():
                    tar.add(p, arcname=f"{self.dir.name}/{p.name}")
        say(f"Results: {self.dir}")
        say(f"Bundle:  {self.bundle}  (send this to Claude)")
        if self.failures:
            say(f"FINISHED WITH {len(self.failures)} FAILURE(S)", RED)
            return 1
        say("Done", GREEN)
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", required=True, help="16 hex digits")
    ap.add_argument("--worker", type=Path, default=ROOT / "build/worker/ksdr-worker")
    ap.add_argument("--results-dir", default=str(ROOT / "window2-results"))
    ap.add_argument("--bundle-dir", default="/tmp")
    ap.add_argument("--long-seconds", type=float, default=600)
    ap.add_argument("--skip-unplug", action="store_true")
    ap.add_argument("--only-unplug", action="store_true", help="open, unplug/replug, normal stop (about 1 minute)")
    ap.add_argument("--only-cpu", action="store_true", help="per-thread CPU in three configurations (about 2 minutes)")
    ap.add_argument("--test", action="store_true", help="mock worker, short timings (development only)")
    ap.add_argument("--short", type=float, default=0.6, help="--test: seconds per timed wait")
    a = ap.parse_args()
    a.serial = int(a.serial.removeprefix("0x"), 16)
    if not a.worker.is_file():
        raise SystemExit(f"{a.worker} not built: run `make probe`")
    probe = subprocess.run([str(a.worker), "--source", "airspyhf"], capture_output=True, text=True)
    if "built without libairspyhf" in probe.stderr:
        raise SystemExit(f"{a.worker} was built without libairspyhf: run `make probe`")
    return asyncio.run(Window2(a).run())


if __name__ == "__main__":
    raise SystemExit(main())
