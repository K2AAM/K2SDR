#!/usr/bin/env python3
"""Window R1: measure the RX888 MkII and record test clips (M2 design §3.3). Run by Adam.

    control/.venv/bin/python tools/rx888/window_r1.py

Drives the pinned rx888_stream (tools/rx888/build_rx888_stream.sh) as a separate process and
reads its raw real int16 samples from a pipe. It never touches PhantomSDR or OpenWebRX: Adam
stops PhantomSDR first, and the script refuses to start while an rx888_stream or spectrumserver
of anyone runs. Mostly unattended (~35 min); it stops for the unplug and the replug.

  R1  versions: streamer commit, firmware sha256, USB link speed
  R2  sustained streaming at each rate (default 32, 50, 64 MS/s): delivered vs expected samples,
      per-second minimum, streamer CPU; plus the streamer's own --measure of the ADC rate
  R3  HF gain: attenuator and VGA steps -> noise floor, WWV 10 MHz carrier level, ADC peak, clipping
  R4  dither and randomizer on/off -> noise floor and the strongest spurs
  R5  clock: WWV 5/10/15 MHz carrier offsets from a 12 s capture -> ppm
  R6  VHF: tuner at each --vhf frequency -> where the signal lands in the ADC band; a clip
  R7  unplug and replug while streaming: what the streamer does, and recovery
  R8  recordings: small complex clips (WWV, 40 m FT8, strongest MW station) for CI fixtures;
      one 2 s raw capture kept on disk (not in the bundle)

Results: rx888-results/<stamp>/ (events.jsonl, summary.json, stderr logs, clips) and a bundle
/tmp/k2-r1-<stamp>.tar.gz (a few MB). Measurements, not verdicts: the analysis happens afterwards.
`--test` runs against tools/rx888/mock_rx888_stream.py at small rates (CI / development only).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).parent))
from rxdsp import carrier_offset, extract_iq, out_rate_for, psd_real, write_sigmf  # noqa: E402

YELLOW, RED, GREEN, RESET = "\033[33m", "\033[31m", "\033[32m", "\033[0m"
VID = "04b4"
PIDS = {"00f3": "bootloader (no firmware)", "00f1": "RX888 (firmware loaded)"}


def say(msg: str, color: str = "") -> None:
    print(f"{color}{msg}{RESET if color else ''}", flush=True)


def usb_devices() -> list[dict]:
    """RX888 devices from sysfs: product id, speed (Mb/s), device node."""
    out = []
    for d in Path("/sys/bus/usb/devices").glob("*"):
        try:
            if (d / "idVendor").read_text().strip() != VID:
                continue
            pid = (d / "idProduct").read_text().strip()
            bus, dev = int((d / "busnum").read_text()), int((d / "devnum").read_text())
            out.append({"pid": pid, "state": PIDS.get(pid, "unknown"), "speed_mbps": (d / "speed").read_text().strip(),
                        "node": f"/dev/bus/usb/{bus:03d}/{dev:03d}"})
        except OSError:
            continue
    return out


def others_running() -> list[str]:
    hits = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit() or int(p.name) == os.getpid():
            continue
        try:
            comm = (p / "comm").read_text().strip()
        except OSError:
            continue
        if comm in ("rx888_stream", "spectrumserver"):
            hits.append(f"{comm} (pid {p.name})")
    return hits


def cpu_seconds(pid: int) -> float | None:
    try:
        f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")
    except OSError:
        return None


class Stream:
    """One rx888_stream run: reads its stdout as fast as possible, counting bytes per second and
    keeping only what was asked (a sample window in memory, or everything into a file)."""

    def __init__(self, argv: list[str], keep: tuple[float, float] | None = None, to_file: Path | None = None,
                 fs: int = 0, stderr_path: Path | None = None) -> None:
        self.fs, self.keep, self.to_file = fs, keep, to_file
        self.err = open(stderr_path, "ab") if stderr_path else subprocess.DEVNULL
        self.p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=self.err, bufsize=0)
        self.t0 = time.monotonic()
        self.bytes = 0
        self.per_second: list[int] = []
        self.kept: list[bytes] = []
        self.last_data = None
        self.done = threading.Event()
        self.th = threading.Thread(target=self._read, daemon=True)
        self.th.start()

    def _read(self) -> None:
        fd = self.p.stdout.fileno()
        sink = open(self.to_file, "wb") if self.to_file else None
        sec_start, sec_bytes = time.monotonic(), 0
        lo, hi = ((int(self.keep[0] * self.fs) * 2, int(self.keep[1] * self.fs) * 2) if self.keep else (0, 0))
        try:
            while True:
                b = os.read(fd, 1 << 22)
                if not b:
                    break
                now = time.monotonic()
                self.last_data = now
                if sink:
                    sink.write(b)
                if self.keep and self.bytes + len(b) > lo and self.bytes < hi:
                    a0, a1 = max(0, lo - self.bytes), min(len(b), hi - self.bytes)
                    self.kept.append(b[a0:a1])
                self.bytes += len(b)
                sec_bytes += len(b)
                if now - sec_start >= 1.0:
                    self.per_second.append(sec_bytes)
                    sec_start, sec_bytes = now, 0
        finally:
            if sink:
                sink.close()
            self.done.set()

    def samples(self) -> np.ndarray:
        raw = b"".join(self.kept)
        return np.frombuffer(raw[: len(raw) // 2 * 2], "<i2")

    def stop(self, timeout: float = 5.0) -> int | None:
        if self.p.poll() is None:
            self.p.send_signal(signal.SIGINT)
            try:
                self.p.wait(timeout)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait(5)
        self.done.wait(5)
        if self.err is not subprocess.DEVNULL:
            self.err.close()
        return self.p.returncode


class WindowR1:
    def __init__(self, a: argparse.Namespace) -> None:
        self.a = a
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = Path(a.results_dir) / stamp
        self.bundle = Path(a.bundle_dir) / f"k2-r1-{stamp}.tar.gz"
        self.dir.mkdir(parents=True, exist_ok=False)
        (self.dir / "clips").mkdir()
        self.events = open(self.dir / "events.jsonl", "a")
        self.summary: dict = {"started_utc": datetime.now(timezone.utc).isoformat(), "test_mode": a.test, "steps": {}}
        self.failures: list[str] = []
        self.fft_n, self.fft_m = (2 ** 14, 256) if a.test else (2 ** 20, 1024)

    # ------------------------------------------------------------------ plumbing
    def log(self, event: str, **kw) -> None:
        self.events.write(json.dumps({"t_utc": datetime.now(timezone.utc).isoformat(), "event": event, **kw}) + "\n")
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
        try:
            import termios
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
        except (ImportError, OSError, ValueError):
            pass
        say(f">> {prompt}", YELLOW)
        note = input("   (press Enter; you can type a note first) ").strip()
        self.log("answer", prompt=prompt, note=note)
        return note

    def argv(self, fs: int, *, att: int = 0, gain: int | None = None, mode: str | None = None, pga: bool | None = None,
             dither: bool = False, randomize: bool = True, vhf: int | None = None, measure: bool = False) -> list[str]:
        gain = self.a.gain if gain is None else gain
        mode = self.a.gain_mode if mode is None else mode
        pga = self.a.pga if pga is None else pga
        v = list(self.a.streamer) + (["vhf", "--frequency", str(vhf)] if vhf else [])
        v += ["-f", str(self.a.firmware), "-s", str(fs), "-g", str(gain), "-m", mode, "-o", "-"]
        if not vhf:
            v += ["-a", str(att)]
        v += (["--pga"] if pga else []) + (["-d"] if dither else []) + (["-r"] if randomize else [])
        return v + (["--measure"] if measure else [])

    def capture(self, fs: int, seconds: float, *, skip: float = 1.0, name: str, **kw) -> tuple[np.ndarray, Stream]:
        """Start, keep `seconds` of samples after `skip` s of settling, stop."""
        s = Stream(self.argv(fs, **kw), keep=(skip, skip + seconds), fs=fs, stderr_path=self.dir / f"{name}.stderr.txt")
        deadline = time.monotonic() + skip + seconds + 15
        while s.bytes < (skip + seconds) * fs * 2 and time.monotonic() < deadline and s.p.poll() is None:
            time.sleep(0.1)
        rc = s.stop()
        x = s.samples()
        if len(x) < 0.9 * seconds * fs:
            raise RuntimeError(f"{name}: got {len(x) / fs:.2f} s of {seconds} s (streamer exit {rc}); "
                               f"see {name}.stderr.txt")
        return x, s

    # --------------------------------------------------------------------- steps
    def r1_versions(self) -> None:
        fw = Path(self.a.firmware)
        commit = (Path(self.a.streamer[-1]).parent / "COMMIT")
        self.step("R1_versions", streamer=" ".join(self.a.streamer),
                  streamer_commit=commit.read_text().strip() if commit.is_file() else None,
                  firmware=str(fw), firmware_sha256=hashlib.sha256(fw.read_bytes()).hexdigest(),
                  usb=usb_devices())

    def r2_throughput(self) -> None:
        rows = []
        for fs in self.a.rates:
            say(f"R2: streaming {fs / 1e6:g} MS/s for {self.a.r2_seconds:g} s ...")
            s = Stream(self.argv(fs), fs=fs, stderr_path=self.dir / f"r2_{fs}.stderr.txt")
            time.sleep(2.0)                                      # firmware load / start-up
            b0, c0, t0 = s.bytes, cpu_seconds(s.p.pid), time.monotonic()
            time.sleep(self.a.r2_seconds)
            b1, c1, t1 = s.bytes, cpu_seconds(s.p.pid), time.monotonic()
            per_sec = s.per_second[2:]
            rc = s.stop()
            expected = 2 * fs * (t1 - t0)
            rows.append({"rate": fs, "seconds": round(t1 - t0, 1), "delivered_ratio": round((b1 - b0) / expected, 5),
                         "min_second_ratio": round(min(per_sec) / (2 * fs), 4) if per_sec else None,
                         "streamer_cpu_pct": round(100 * (c1 - c0) / (t1 - t0), 1) if c0 is not None and c1 is not None else None,
                         "exit": rc})
        say("R2: the streamer's own ADC-rate measurement (20 s) ...")
        fs = max(self.a.rates)
        s = Stream(self.argv(fs, measure=True), fs=fs, stderr_path=self.dir / "r2_measure.stderr.txt")
        time.sleep(self.a.measure_seconds)
        s.stop()
        tail = (self.dir / "r2_measure.stderr.txt").read_text(errors="replace").strip().splitlines()[-5:]
        self.step("R2_throughput", rows=rows, measure_tail=tail)

    def _hf_levels(self, x: np.ndarray, fs: int) -> dict:
        f, p = psd_real(x, fs, self.a.psd_n)
        wwv = self.a.wwv[1] if len(self.a.wwv) > 1 else self.a.wwv[0]
        near = (np.abs(f - wwv) < 50_000) & (np.abs(f - wwv) > 2 * fs / self.a.psd_n)
        k = int(np.argmin(np.abs(f - wwv)))
        floor = float(np.median(p[near])) if near.any() else float("nan")
        carrier = float(p[max(0, k - 2):k + 3].max())
        peak = int(np.max(np.abs(x.astype(np.int32))))
        return {"floor_dbfs_per_bin": round(floor, 1), "wwv_carrier_dbfs": round(carrier, 1),
                "carrier_over_floor_db": round(carrier - floor, 1), "adc_peak": peak,
                "clipped_fraction": float(np.mean(np.abs(x.astype(np.int32)) >= 32767))}

    def r3_gain(self) -> None:
        fs, rows = self.a.gain_rate, []
        sets = [{"att": a} for a in self.a.att_steps] + [{"gain": g} for g in self.a.vga_steps] + \
               [{"pga": False}, {"pga": True}, {"mode": "low"}, {"mode": "high"}]
        for kw in sets:
            say(f"R3: {kw} ...")
            try:
                x, _ = self.capture(fs, self.a.gain_seconds, name=f"r3_{'_'.join(f'{k}{v}' for k, v in kw.items())}", **kw)
                rows.append({**kw, **self._hf_levels(x, fs)})
            except RuntimeError as e:
                rows.append({**kw, "error": str(e)})
        self.step("R3_gain", rate=fs, defaults={"gain": self.a.gain, "mode": self.a.gain_mode, "pga": self.a.pga},
                  rows=rows)

    def r4_dither(self) -> None:
        fs, rows = self.a.gain_rate, []
        for dither in (False, True):
            for rnd in (False, True):
                x, _ = self.capture(fs, self.a.gain_seconds, name=f"r4_d{int(dither)}_r{int(rnd)}", dither=dither,
                                    randomize=rnd)
                f, p = psd_real(x, fs, self.a.psd_n)
                med = float(np.median(p))
                band = f > 100_000
                idx = np.argsort(p[band])[::-1]
                spurs, ff, pp = [], f[band], p[band]
                for i in idx:
                    if len(spurs) >= 10:
                        break
                    if all(abs(ff[i] - s[0]) > 5 * fs / self.a.psd_n for s in spurs):
                        spurs.append((int(ff[i]), round(float(pp[i]) - med, 1)))
                rows.append({"dither": dither, "randomize": rnd, "median_dbfs_per_bin": round(med, 1),
                             "top_peaks_hz_db_over_median": spurs})
        self.step("R4_dither_randomizer", rate=fs, rows=rows)

    def r5_clock(self) -> None:
        fs = self.a.clock_rate
        raw = self.dir / "r5_raw.bin"
        say(f"R5: {self.a.clock_seconds:g} s at {fs / 1e6:g} MS/s to disk for the WWV carriers ...")
        s = Stream(self.argv(fs), to_file=raw, fs=fs, stderr_path=self.dir / "r5.stderr.txt")
        deadline = time.monotonic() + self.a.clock_seconds + 20
        while s.bytes < (self.a.clock_seconds + 1) * fs * 2 and time.monotonic() < deadline and s.p.poll() is None:
            time.sleep(0.2)
        s.stop()
        x = np.memmap(raw, "<i2", mode="r")[fs:]                 # skip the first second
        rate = out_rate_for(fs, self.fft_n, self.fft_m)
        rows = []
        for f0 in self.a.wwv:
            iq = extract_iq(x, fs, f0, self.fft_n, self.fft_m)
            off, snr = carrier_offset(iq, rate)
            rows.append({"wwv_hz": f0, "offset_hz": round(off, 4), "ppm": round(-off / f0 * 1e6, 4) if snr > 20 else None,
                         "line_over_median_db": round(snr, 1), "seconds": round(len(iq) / rate, 2)})
        del x
        raw.unlink()
        self.step("R5_clock", rate=fs, note="ppm = ADC clock error: positive when the ADC runs fast", rows=rows)

    def r6_vhf(self) -> None:
        fs, rows = self.a.clock_rate, []
        for fv in self.a.vhf:
            say(f"R6: VHF tuner at {fv} Hz ...")
            try:
                x, _ = self.capture(fs, self.a.vhf_seconds, name=f"r6_{fv}", vhf=fv)
            except RuntimeError as e:
                rows.append({"tuner_hz": fv, "error": str(e)})
                continue
            f, p = psd_real(x, fs, self.a.psd_n)
            med = float(np.median(p))
            k = int(np.argmax(p[1:])) + 1
            peak_hz = int(round(f[k] / 1000)) * 1000                # where the signal lands (IF)
            m = self.fft_m * (8 if fv < 108_000_000 else 1)          # WFM needs ~250 kS/s
            rate = out_rate_for(fs, self.fft_n, m)
            iq = extract_iq(x, fs, peak_hz, self.fft_n, m, max_out=int(rate * min(5, self.a.vhf_seconds - 0.5)))
            base = self.dir / "clips" / f"rx888_vhf_{fv}"
            write_sigmf(base, iq, rate, peak_hz,
                        f"RX888 MkII VHF input, tuner at {fv} Hz, ADC {fs} S/s real; extracted around the strongest "
                        f"line at {peak_hz} Hz in the ADC band (tuner IF, not RF), {len(iq) / rate:.1f} s",
                        "RX888 MkII")
            rows.append({"tuner_hz": fv, "strongest_adc_hz": peak_hz, "over_median_db": round(float(p[k]) - med, 1),
                         "clip": base.name, "clip_rate": rate})
        self.step("R6_vhf", rate=fs, rows=rows)

    def r7_unplug(self) -> None:
        fs = self.a.gain_rate
        s = Stream(self.argv(fs), fs=fs, stderr_path=self.dir / "r7.stderr.txt")
        time.sleep(3)
        self.ask("UNPLUG the RX888's USB cable NOW, then press Enter")
        t_pulled = time.monotonic()
        stopped_at, exit_at = None, None
        while time.monotonic() - t_pulled < self.a.unplug_wait:
            if s.p.poll() is not None and exit_at is None:
                exit_at = time.monotonic()
            if s.last_data is not None and time.monotonic() - s.last_data > 1.0 and stopped_at is None:
                stopped_at = s.last_data
            if exit_at is not None:
                break
            time.sleep(0.1)
        hung = s.p.poll() is None
        rc = s.stop(timeout=5)
        result = {"data_stopped_s_after_enter": round(stopped_at - t_pulled, 2) if stopped_at else None,
                  "exited_by_itself": not hung, "exit_s_after_enter": round(exit_at - t_pulled, 2) if exit_at else None,
                  "exit_code": rc, "stderr_tail": (self.dir / "r7.stderr.txt").read_text(errors="replace").splitlines()[-5:]}
        self.ask("Plug the RX888 back in (same USB 3 port), wait 3 seconds, then press Enter")
        t0 = time.monotonic()
        while time.monotonic() - t0 < 60 and not (self.a.test or usb_devices()):
            time.sleep(0.5)
        result["enumerated_s"] = round(time.monotonic() - t0, 1)
        result["usb_after"] = usb_devices()
        try:
            x, _ = self.capture(fs, 3.0, name="r7_recovery")
            result["recovery_stream_ok"] = True
            result["recovery_levels"] = self._hf_levels(x, fs)
        except RuntimeError as e:
            result["recovery_stream_ok"] = False
            result["recovery_error"] = str(e)
        self.step("R7_unplug", **result)

    def r8_recordings(self) -> None:
        fs = self.a.record_rate
        raw = self.dir / "r8_raw.bin"
        say(f"R8: {self.a.record_seconds:g} s at {fs / 1e6:g} MS/s for the clips ...")
        s = Stream(self.argv(fs), to_file=raw, fs=fs, stderr_path=self.dir / "r8.stderr.txt")
        deadline = time.monotonic() + self.a.record_seconds + 20
        while s.bytes < (self.a.record_seconds + 1) * fs * 2 and time.monotonic() < deadline and s.p.poll() is None:
            time.sleep(0.2)
        s.stop()
        x = np.memmap(raw, "<i2", mode="r")[fs:]
        rate = out_rate_for(fs, self.fft_n, self.fft_m)
        f, p = psd_real(x[: fs * 2], fs, self.a.psd_n)
        mw = (f >= self.a.mw_band[0]) & (f <= self.a.mw_band[1])
        mw_hz = int(round(f[mw][int(np.argmax(p[mw]))] / 10_000) * 10_000) if mw.any() else None
        rows = []
        targets = [("wwv10", self.a.wwv[1] if len(self.a.wwv) > 1 else self.a.wwv[0]), ("ft8_40m", self.a.ft8_hz)]
        if mw_hz:
            targets.append(("mw", mw_hz))
        for name, fc in targets:
            iq = extract_iq(x, fs, fc, self.fft_n, self.fft_m, max_out=int(rate * self.a.clip_seconds))
            base = self.dir / "clips" / f"rx888_{name}_{fc}"
            write_sigmf(base, iq, rate, fc, f"RX888 MkII HF, ADC {fs} S/s real, extracted around {fc} Hz, "
                                            f"{len(iq) / rate:.1f} s", "RX888 MkII")
            rows.append({"name": name, "center_hz": fc, "rate": rate, "seconds": round(len(iq) / rate, 2), "clip": base.name})
        keep = self.dir / "r8_raw_2s.bin"                         # kept on the Optiplex for development
        with open(keep, "wb") as fh:
            fh.write(np.asarray(x[: 2 * fs]).tobytes())
        del x
        raw.unlink()
        self.step("R8_recordings", rate=fs, clips=rows, raw_kept=str(keep), raw_bytes=keep.stat().st_size)

    # ----------------------------------------------------------------------- run
    def preflight(self) -> None:
        if not self.a.test:
            busy = others_running()
            if busy:
                raise SystemExit(f"Stop PhantomSDR first: {', '.join(busy)} running. Nothing was started.")
            devs = usb_devices()
            if not devs:
                raise SystemExit("No RX888 on USB (vendor 04b4). Check the cable and port; nothing was started.")
            for d in devs:
                if not os.access(d["node"], os.W_OK):
                    raise SystemExit(f"{d['node']} is not writable by {os.environ.get('USER')}: the RX888 udev rule "
                                     "does not cover this account. Nothing was started.")
        for p in (Path(self.a.firmware), Path(self.a.streamer[-1])):
            if not p.exists():
                raise SystemExit(f"{p} missing: run tools/rx888/build_rx888_stream.sh first")

    def run(self) -> int:
        self.preflight()
        steps = [("R1_versions", self.r1_versions), ("R2_throughput", self.r2_throughput), ("R3_gain", self.r3_gain),
                 ("R4_dither_randomizer", self.r4_dither), ("R5_clock", self.r5_clock), ("R6_vhf", self.r6_vhf),
                 ("R8_recordings", self.r8_recordings), ("R7_unplug", self.r7_unplug)]
        only = set(self.a.only or [])
        for name, fn in steps:
            if only and not any(name.startswith(o) for o in only):
                continue
            say(f"== {name}", GREEN)
            try:
                fn()
            except Exception as e:                                # noqa: BLE001 - record and continue
                self.fail(name, f"{type(e).__name__}: {e}")
        self.summary["finished_utc"] = datetime.now(timezone.utc).isoformat()
        self.summary["failures"] = self.failures
        (self.dir / "summary.json").write_text(json.dumps(self.summary, indent=2) + "\n")
        self.events.close()
        with tarfile.open(self.bundle, "w:gz") as tar:
            for p in sorted(self.dir.rglob("*")):
                if p.is_file() and not p.name.endswith(".bin"):
                    tar.add(p, arcname=str(p.relative_to(self.dir.parent)))
        say(f"Bundle: {self.bundle} ({self.bundle.stat().st_size / 1e6:.1f} MB) — send this to Claude", GREEN)
        say("Done" if not self.failures else f"FINISHED WITH {len(self.failures)} FAILURE(S)", GREEN if not self.failures else RED)
        return 0 if not self.failures else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    third = ROOT / "build/third_party/rx888_stream"
    ap.add_argument("--streamer", nargs="+", default=[str(third / "rx888_stream")])
    ap.add_argument("--firmware", default=str(third / "SDDC_FX3.img"))
    ap.add_argument("--rates", type=int, nargs="+", default=[32_000_000, 50_000_000, 64_000_000])
    ap.add_argument("--r2-seconds", type=float, default=600.0, help="per rate (default 10 min)")
    ap.add_argument("--measure-seconds", type=float, default=20.0)
    ap.add_argument("--gain-rate", type=int, default=64_000_000)
    ap.add_argument("--gain-seconds", type=float, default=0.5)
    ap.add_argument("--gain", type=int, default=32)
    ap.add_argument("--gain-mode", default="low", choices=["high", "low"])
    ap.add_argument("--pga", action="store_true", default=False)
    ap.add_argument("--att-steps", type=int, nargs="+", default=[0, 8, 16, 24, 32, 48, 63])
    ap.add_argument("--vga-steps", type=int, nargs="+", default=[0, 16, 32, 64, 96, 127])
    ap.add_argument("--psd-n", type=int, default=65536)
    ap.add_argument("--wwv", type=int, nargs="+", default=[5_000_000, 10_000_000, 15_000_000])
    ap.add_argument("--clock-rate", type=int, default=32_000_000)
    ap.add_argument("--clock-seconds", type=float, default=12.0)
    ap.add_argument("--vhf", type=int, nargs="*", default=[162_550_000, 100_300_000])
    ap.add_argument("--vhf-seconds", type=float, default=4.0)
    ap.add_argument("--record-rate", type=int, default=64_000_000)
    ap.add_argument("--record-seconds", type=float, default=9.0)
    ap.add_argument("--clip-seconds", type=float, default=8.0)
    ap.add_argument("--ft8-hz", type=int, default=7_075_500, help="clip centre for 40 m FT8 (dial 7.074 MHz + 1.5 kHz)")
    ap.add_argument("--mw-band", type=int, nargs=2, default=[530_000, 1_700_000])
    ap.add_argument("--unplug-wait", type=float, default=20.0, help="seconds to watch the streamer after the unplug")
    ap.add_argument("--only", nargs="*", help="run only these steps (prefixes, e.g. R2 R5)")
    ap.add_argument("--results-dir", default=str(ROOT / "rx888-results"))
    ap.add_argument("--bundle-dir", default="/tmp")
    ap.add_argument("--test", action="store_true", help="mock streamer, small rates (CI only)")
    a = ap.parse_args()
    if a.test:
        mock = Path(__file__).with_name("mock_rx888_stream.py")
        a.streamer = [sys.executable, str(mock)]
        a.firmware = str(mock)                                    # any existing file
        fs = 2 ** 21
        a.rates, a.gain_rate, a.clock_rate, a.record_rate = [fs], fs, fs, fs
        a.r2_seconds, a.measure_seconds, a.gain_seconds, a.clock_seconds = 2.0, 1.5, 0.3, 3.0
        a.vhf_seconds, a.record_seconds, a.clip_seconds, a.psd_n = 2.0, 3.0, 1.5, 8192
        a.unplug_wait = 2.0
        a.att_steps, a.vga_steps = [0, 20], [16, 64]
        a.wwv, a.vhf, a.ft8_hz, a.mw_band = [250_000, 400_000, 600_000], [162_550_000], 700_000, [800_000, 1_000_000]
    return WindowR1(a).run()


if __name__ == "__main__":
    sys.exit(main())
