"""tools/b2/make_clips.py finds the right signals in synthetic stand-ins for the Window 1 recordings."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]


def write(d: Path, name: str, x: np.ndarray, rate: int, center: int) -> None:
    x = x / np.max(np.abs(x)) * 0.5
    iq = np.empty(2 * len(x), "<i2")
    iq[0::2], iq[1::2] = np.round(x.real * 32767), np.round(x.imag * 32767)
    iq.tofile(d / f"{name}.sigmf-data")
    (d / f"{name}.sigmf-meta").write_text(json.dumps({
        "global": {"core:datatype": "ci16_le", "core:sample_rate": rate, "core:version": "1.2.6", "core:sha512": "0" * 128},
        "captures": [{"core:sample_start": 0, "core:frequency": center, "core:datetime": "2026-10-26T20:00:00.000Z"}],
        "annotations": []}))


def test_make_clips_picks_each_signal(tmp_path):
    rng = np.random.default_rng(1)
    rec = tmp_path / "rec"
    rec.mkdir()
    noise = lambda n: (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * 0.01   # noqa: E731
    r = 912_000
    t = np.arange(r * 7) / r                                    # the MW clip is 1 s + 5 s
    fm = noise(len(t))
    for off, a in ((200_000, 1.0), (0, 0.2)):                  # 100.3 MHz strong, 100.1 weaker
        fm += a * np.exp(1j * (2 * np.pi * off * t + np.cumsum(np.sin(2 * np.pi * 1000 * t) * 50_000) * 2 * np.pi / r))
    write(rec, "fm", fm, r, 100_100_000)
    mw = noise(len(t))
    for off, a in ((-120_000, 1.0), (10_000, 0.3)):             # 880 kHz strong
        mw += a * (1 + 0.5 * np.sin(2 * np.pi * 400 * t)) * np.exp(2j * np.pi * off * t)
    write(rec, "mw", mw, r, 1_000_000)
    r = 192_000
    t = np.arange(r * 10) / r
    x = noise(len(t))
    x += 0.3 * (np.sin(2 * np.pi * 3 * t) > 0) * np.exp(2j * np.pi * (7_030_000 - 7_120_000) * t)   # keyed CW
    b = rng.standard_normal(len(t)) + 1j * rng.standard_normal(len(t))
    spec, f = np.fft.fft(b), np.fft.fftfreq(len(t), 1 / r)
    spec[~((f > -2700) & (f < -300))] = 0
    env = np.clip(np.sin(2 * np.pi * 4 * t), 0, None)            # syllabic bursts
    x += 2.0 * env * np.fft.ifft(spec) * np.exp(2j * np.pi * (7_150_000 - 7_120_000) * t)
    write(rec, "40m", x, r, 7_120_000)
    out = tmp_path / "out"
    run = subprocess.run([sys.executable, str(ROOT / "tools/b2/make_clips.py"), "--rec-dir", str(rec), "--out", str(out)],
                         capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout + run.stderr
    s = json.loads((out / "summary.json").read_text())
    assert {k: v["frequency_hz"] for k, v in s.items()} == {"wfm": 100_300_000, "am_mw": 880_000, "cw": 7_030_000,
                                                            "lsb": 7_150_000}
    assert s["cw"]["clip_center_hz"] == 7_049_000                 # kept inside the 40 m span
    for v in s.values():
        meta = json.loads((out / f"{v['clip']}.sigmf-meta").read_text())
        assert meta["global"]["core:sample_rate"] == v["rate"]
        assert meta["captures"][0]["core:frequency"] == v["clip_center_hz"]
    Path(run.stdout.split("Bundle: ")[1].split(" ")[0]).unlink()
