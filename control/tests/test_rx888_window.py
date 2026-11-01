"""tools/rx888/window_r1.py end to end against the mock rx888_stream, and the extractor."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/rx888"))
from rxdsp import carrier_offset, extract_iq, out_rate_for  # noqa: E402


def test_extract_iq_is_exact_continuous_and_integer_hz():
    fs, n, m = 2 ** 21, 2 ** 14, 256
    rate = out_rate_for(fs, n, m)
    t = np.arange(fs * 2) / fs
    x = 0.3 * 32767 * np.cos(2 * np.pi * (300_000 + 1000.25) * t) + 0.3 * 32767 * np.cos(2 * np.pi * 340_000 * t)
    y = extract_iq(np.round(x).astype("<i2"), fs, 300_000, n, m)            # 340 kHz is outside the band
    off, _ = carrier_offset(y, rate)
    assert abs(off - 1000.25) < 0.01
    amp = np.abs(y[50:-50]) / 32767
    assert abs(amp.mean() - 0.3) < 0.002 and amp.std() < 1e-3                # no seams between blocks
    inst = np.diff(np.unwrap(np.angle(y))) * rate / (2 * np.pi)
    assert np.abs(inst - 1000.25).max() < 20


def test_window_r1_runs_against_the_mock(tmp_path):
    env = {**os.environ, "MOCK_TONES": "250000:-20,400000:-20,600000:-20,900000:-25,700000:-30", "MOCK_PPM": "1.5"}
    run = subprocess.run([sys.executable, str(ROOT / "tools/rx888/window_r1.py"), "--test",
                          "--results-dir", str(tmp_path / "res"), "--bundle-dir", str(tmp_path)],
                         env=env, capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-3000:]
    (res,) = (tmp_path / "res").iterdir()
    s = json.loads((res / "summary.json").read_text())["steps"]
    assert not any("error" in v for v in s.values()), s
    assert 0.95 < s["R2_throughput"]["rows"][0]["delivered_ratio"] < 1.05
    ppms = [r["ppm"] for r in s["R5_clock"]["rows"]]
    assert all(abs(p - 1.5) < 0.1 for p in ppms), ppms                       # the mock's clock error
    att = {r["att"]: r["wwv_carrier_dbfs"] for r in s["R3_gain"]["rows"] if "att" in r}
    assert 8 < att[0] - att[20] < 12                                         # mock: 0.5 dB per step
    assert s["R6_vhf"]["rows"][0]["strongest_adc_hz"] == 300_000
    clips = {c["name"]: c for c in s["R8_recordings"]["clips"]}
    assert {"wwv10", "ft8_40m"} <= set(clips)
    meta = json.loads((res / "clips" / f"{clips['wwv10']['clip']}.sigmf-meta").read_text())
    assert meta["captures"][0]["core:frequency"] == 400_000 and meta["global"]["core:sample_rate"] == 32768
    assert s["R7_unplug"]["recovery_stream_ok"] is True
    (bundle,) = tmp_path.glob("k2-r1-*.tar.gz")
    with tarfile.open(bundle) as t:
        names = t.getnames()
    assert not any(n.endswith(".bin") for n in names) and any(n.endswith("summary.json") for n in names)
