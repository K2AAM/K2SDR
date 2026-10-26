"""Window 1 tooling, exercised without hardware via the probe linked to a mock libairspyhf.

Covers every path the real window will use: info, busy-check, stream (rate,
drops, unplug), record (+ retune captures and drop annotations), finalize,
carrier accuracy and settle analysis, and fixture clipping.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from k2websdr.replay import sigmf as k2sigmf

ROOT = Path(__file__).resolve().parents[2]
MOCK = ROOT / "build/worker/ksdr-airspyhf-probe-mock"
TOOLS = ROOT / "tools/window1"
SERIAL = "A1B2C3D4E5F60718"

if not MOCK.exists():
    if os.environ.get("K2_REQUIRE_PROBE") == "1":
        raise RuntimeError(f"{MOCK} missing; build with libairspyhf-dev installed")
    pytest.skip("probe not built (libairspyhf-dev not installed)", allow_module_level=True)


def probe(*args, env=None, timeout=30) -> tuple[int, list[dict]]:
    e = dict(os.environ, **(env or {}))
    r = subprocess.run([str(MOCK), *args], capture_output=True, text=True, timeout=timeout, env=e)
    return r.returncode, [json.loads(line) for line in r.stdout.splitlines() if line.startswith("{")]


def events(lines, name):
    return [x for x in lines if x["event"] == name]


def tool(name, *args) -> list[dict]:
    r = subprocess.run([sys.executable, str(TOOLS / name), *map(str, args)], capture_output=True, text=True,
                       timeout=120, check=True)
    return [json.loads(line) for line in r.stdout.splitlines() if line.startswith("{")]


def test_info_reports_device_and_rates():
    code, out = probe("info", "--serial", SERIAL)
    assert code == 0
    assert events(out, "devices")[0]["serials"] == [SERIAL]
    rates = events(out, "samplerates")[0]["rates_hz"]
    assert 768000 in rates and len(events(out, "samplerate_mode")) == len(rates)
    assert events(out, "calibration")[0]["result"] == 0


def test_busy_check_reports_without_configuring():
    code, out = probe("busy-check", env={"MOCK_BUSY": "1"})
    assert code == 0
    att = events(out, "open_attempt")[0]
    assert (att["opened"], att["result"]) == (False, -1)
    code, out = probe("busy-check")
    assert events(out, "open_attempt")[0]["opened"] is True


def test_open_failure_and_bad_rate_exit_codes():
    code, out = probe("stream", "--serial", SERIAL, "--freq", "10000000", "--rate", "768000", "--seconds", "1",
                      env={"MOCK_BUSY": "1"})
    assert code == 2 and events(out, "open_failed")
    code, out = probe("stream", "--serial", SERIAL, "--freq", "10000000", "--rate", "123", "--seconds", "1")
    assert code == 3 and events(out, "set_samplerate")[0]["result"] == -1


def test_stream_measures_rate_and_counts_drops():
    code, out = probe("stream", "--serial", SERIAL, "--freq", "10000000", "--rate", "192000", "--seconds", "2",
                      "--hf-att", "3", "--hf-agc", "0", env={"MOCK_DROP_AT_S": "0.5", "MOCK_DROP_SAMPLES": "1500"})
    assert code == 0
    s = events(out, "summary")[0]
    assert abs(s["rate_measured"] - 192000) / 192000 < 0.02
    assert s["dropped_total"] == 1500 and s["device_lost"] is False
    assert {x["name"]: x["result"] for x in events(out, "setting")} == {"hf_agc": 0, "hf_att": 0}


def test_unplug_is_detected_and_reported():
    code, out = probe("stream", "--serial", SERIAL, "--freq", "10000000", "--rate", "192000", "--seconds", "6",
                      env={"MOCK_UNPLUG_AT_S": "1.2"})
    assert code == 4
    assert events(out, "streaming_stopped")
    assert events(out, "summary")[0]["device_lost"] is True


@pytest.fixture(scope="module")
def retune_recording(tmp_path_factory) -> Path:
    base = tmp_path_factory.mktemp("w1") / "retune"
    code, out = probe("record", "--serial", SERIAL, "--freq", "10000000", "--rate", "192000", "--seconds", "3",
                      "--retune", "15000000@1.0", "--retune", "10000000@2.0", "--out", str(base),
                      env={"MOCK_SETTLE_MS": "40", "MOCK_DROP_AT_S": "2.5", "MOCK_DROP_SAMPLES": "800"})
    assert code == 0, out
    assert [r["result"] for r in events(out, "retune")] == [0, 0]
    tool("finalize.py", base)
    return base


def test_recording_is_valid_sigmf_with_captures_and_drop_annotation(retune_recording):
    meta = json.loads(Path(str(retune_recording) + ".sigmf-meta").read_text())
    g = meta["global"]
    assert g["core:datatype"] == "cf32_le" and g["core:sample_rate"] == 192000 and len(g["core:sha512"]) == 128
    assert [c["core:frequency"] for c in meta["captures"]] == [10000000, 15000000, 10000000]
    (ann,) = meta["annotations"]
    assert ann["core:label"] == "discontinuity" and ann["k2websdr:gap_samples"] == 800


def test_carrier_accuracy_analysis(retune_recording):
    res = tool("analyze.py", "carrier", retune_recording, "--expect", 10005000, "--expect", 15005000)
    assert {r["expected_hz"] for r in res} == {10005000, 15005000}
    assert all(abs(r["error_ppm"]) < 0.5 and r["usable"] and r["peak_snr_db"] > 30 for r in res), res


def test_carrier_analysis_refuses_to_trust_noise(retune_recording):
    # No tone exists near 10.050 MHz in this recording: whatever peak is found must be flagged unusable.
    res = tool("analyze.py", "carrier", retune_recording, "--expect", 10050000)
    assert res and all(r["usable"] is False and "warning" in r for r in res), res


def test_settle_analysis_finds_the_simulated_settling(retune_recording):
    res = tool("analyze.py", "settle", retune_recording, "--expect", 10005000, "--expect", 15005000)
    assert len(res) == 2
    for r in res:
        assert r["settle_ms"] is not None and 25 <= r["settle_ms"] <= 70, res


def test_clip_produces_a_loadable_fixture_with_the_tone_where_expected(retune_recording, tmp_path):
    out = tmp_path / "clip"
    tool("clip.py", retune_recording, out, "--center", 10004000, "--rate", 48000, "--start-s", 0.2,
         "--seconds", 0.6)
    rec = k2sigmf.load(Path(str(out) + ".sigmf-meta"), require_sha512=True)
    assert (rec.sample_rate_hz, rec.freq_origin_hz) == (48000, 10004000)
    raw = np.fromfile(rec.data_path, dtype="<i2").astype(float)
    x = raw[0::2] + 1j * raw[1::2]
    f = np.fft.fftfreq(len(x), 1 / 48000)[np.argmax(np.abs(np.fft.fft(x * np.hanning(len(x)))))]
    assert abs(f - 1000) < 50                       # 10.005 MHz tone at +1 kHz from the 10.004 MHz center


def test_full_window_rehearsal(tmp_path):
    """Run tools/window1/run_window1.sh end to end against the mock, answering every prompt."""
    env = dict(os.environ, W1_TEST="1", W1_PROBE=str(MOCK), W1_RATE_S="1", W1_LONG_S="1", W1_REC_S="3",
               W1_UNPLUG_STREAM_S="4", W1_UNPLUG_ENV="MOCK_UNPLUG_AT_S=1.5", W1_UNPLUG_PROMPT_S="1",
               W1_REPLUG_WAIT_S="0", W1_CLIP_START="0.5", W1_CLIP_LEN="1", TMPDIR=str(tmp_path))
    r = subprocess.run(["bash", str(ROOT / "tools/window1/run_window1.sh")], input="\n" * 40, text=True,
                       capture_output=True, timeout=240, env=env, cwd=ROOT)
    try:
        assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
        assert "exit code: 4" in r.stdout                       # unplug detected
        bundle = next(Path("/tmp").glob("k2-window1-*.tar.gz"))
        names = subprocess.run(["tar", "tzf", str(bundle)], capture_output=True, text=True).stdout
        for f in ("airspyhf_wwv10_48k.sigmf-meta", "airspyhf_ft8_40m_48k.sigmf-data", "window1.jsonl",
                  "owrx-before.json", "owrx-after.json"):
            assert f in names
        log = [json.loads(x) for x in (ROOT / "window1-results").glob("*/window1.jsonl").__next__().read_text()
               .splitlines() if x.startswith("{")]
        assert any(e.get("event") == "operator" for e in log)    # prompts and answers are recorded
        assert sum(1 for e in log if e.get("event") == "summary" and e.get("mode") == "record") == 7
    finally:
        import shutil
        shutil.rmtree(ROOT / "window1-results", ignore_errors=True)
        for p in Path("/tmp").glob("k2-window1-*"):
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink()
