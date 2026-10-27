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


def test_retune_analysis_finds_the_step_and_the_simulated_transient(retune_recording):
    res = tool("analyze.py", "retune", retune_recording, "--expect", 10005000, "--expect", 15005000)
    assert len(res) == 2
    for r in res:
        assert r["step_ms"] is not None and abs(r["step_ms"]) <= 12, res     # within ~1 block of the call
        assert 25 <= r["settle_ms"] <= 70, res                                 # mock: 40 ms of junk
        assert r["carrier_present"] and abs(abs(r["carrier_offset_hz"]) - 5000) < 2, res


def test_retune_analysis_reports_no_transient_for_a_clean_switch(tmp_path):
    base = tmp_path / "clean"
    code, _ = probe("record", "--serial", SERIAL, "--freq", "10000000", "--rate", "192000", "--seconds", "2",
                    "--retune", "15000000@0.8", "--out", str(base), env={"MOCK_SETTLE_MS": "0"})
    assert code == 0
    (r,) = tool("analyze.py", "retune", base, "--expect", 10005000, "--expect", 15005000)
    assert r["settle_ms"] == 0.0, r


def test_clip_datetime_is_the_clip_start_not_the_recording_start(retune_recording, tmp_path):
    out = tmp_path / "late_clip"
    tool("clip.py", retune_recording, out, "--center", 10004000, "--rate", 48000, "--start-s", 2.1, "--seconds", 0.3)
    import datetime as dt
    src = json.loads(Path(str(retune_recording) + ".sigmf-meta").read_text())["captures"]
    clip = json.loads(Path(str(out) + ".sigmf-meta").read_text())["captures"][0]["core:datetime"]
    third = src[2]                                   # the clip lies in the third capture (10 MHz from ~2.0 s)
    parse = lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    expect = parse(third["core:datetime"]) + dt.timedelta(seconds=(int(2.1 * 192000) - third["core:sample_start"]) / 192000)
    assert abs((parse(clip) - expect).total_seconds()) < 0.002, (clip, expect)


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


# --------------------------------------------------------------- review fixes (PR #14 follow-up)

def test_record_reports_disk_write_failure_and_leaves_no_metadata(tmp_path):
    import resource

    base = tmp_path / "full"
    limit = 100 * 1024

    def small_files():
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))

    r = subprocess.run([str(MOCK), "record", "--serial", SERIAL, "--freq", "10000000", "--rate", "192000",
                        "--seconds", "1", "--out", str(base)], capture_output=True, text=True, timeout=30,
                       preexec_fn=small_files)
    out = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
    assert r.returncode == 5, r.stdout
    (summ,) = events(out, "summary")
    assert summ["recording_ok"] is False
    assert summ["written_samples"] * 8 <= limit < summ["samples"] * 8      # never claims unwritten samples
    assert "File too large" in events(out, "recording_failed")[0]["error"]
    assert not Path(str(base) + ".sigmf-meta").exists()                  # cannot pass for a valid recording


def test_consecutive_library_drops_are_per_callback_not_cumulative(tmp_path):
    # libairspyhf 1.6.8 reports the loss before each block; three losses in a row must all count.
    pattern = {"MOCK_DROP_AT_S": "0.5", "MOCK_DROP_PATTERN": "1000,1000,500"}
    code, out = probe("stream", "--serial", SERIAL, "--freq", "10000000", "--rate", "192000", "--seconds", "1",
                      env=pattern)
    assert code == 0 and events(out, "summary")[0]["dropped_total"] == 2500
    base = tmp_path / "drops"
    code, out = probe("record", "--serial", SERIAL, "--freq", "10000000", "--rate", "192000", "--seconds", "1",
                      "--out", str(base), env=pattern)
    assert code == 0 and events(out, "summary")[0]["dropped_total"] == 2500
    ann = json.loads(Path(str(base) + ".sigmf-meta").read_text())["annotations"]
    assert [a["k2websdr:gap_samples"] for a in ann] == [1000, 1000, 500]
    assert ann[1]["core:sample_start"] - ann[0]["core:sample_start"] == 1024   # one block apart


def test_writer_overflow_is_counted_and_annotated_as_a_gap(tmp_path):
    """A stalled disk (here: a pipe nobody reads for 1.5 s) must leave an annotated gap, not a silent splice."""
    import threading
    import time

    base = tmp_path / "slow"
    fifo = Path(str(base) + ".sigmf-data")
    os.mkfifo(fifo)
    got = [0]

    def reader():
        with open(fifo, "rb") as f:
            time.sleep(1.5)
            while b := f.read(1 << 20):
                got[0] += len(b)

    t = threading.Thread(target=reader)
    t.start()
    code, out = probe("record", "--serial", SERIAL, "--freq", "10000000", "--rate", "912000", "--seconds", "3",
                      "--out", str(base))
    t.join(30)
    (summ,) = events(out, "summary")
    assert code == 0 and summ["recording_ok"] is True
    assert summ["queue_overflow_samples"] > 0
    assert summ["written_samples"] + summ["queue_overflow_samples"] == summ["samples"]
    assert got[0] == summ["written_samples"] * 8
    ann = json.loads(Path(str(base) + ".sigmf-meta").read_text())["annotations"]
    assert sum(a["k2websdr:gap_samples"] for a in ann) == summ["queue_overflow_samples"]


def test_rejected_retune_does_not_start_a_capture(tmp_path):
    base = tmp_path / "bad_retune"
    code, out = probe("record", "--serial", SERIAL, "--freq", "10000000", "--rate", "192000", "--seconds", "2",
                      "--retune", "300000000@0.5", "--retune", "15000000@1.0", "--out", str(base))
    assert code == 0
    assert [r["result"] for r in events(out, "retune")] == [-1, 0]       # mock rejects > 260 MHz
    caps = json.loads(Path(str(base) + ".sigmf-meta").read_text())["captures"]
    assert [c["core:frequency"] for c in caps] == [10000000, 15000000]
    assert events(out, "summary")[0]["captures"] == 2


def clip_run(src, out, *args):
    return subprocess.run([sys.executable, str(TOOLS / "clip.py"), str(src), str(out), *map(str, args)],
                          capture_output=True, text=True, timeout=120)


def test_clip_refuses_to_cross_a_discontinuity(retune_recording, tmp_path):
    # The fixture recording lost 800 samples at 2.5 s (third capture, 10 MHz, starts at 2.0 s).
    out = tmp_path / "gap_clip"
    r = clip_run(retune_recording, out, "--center", 10004000, "--rate", 48000, "--start-s", 2.3, "--seconds", 0.4)
    assert r.returncode != 0 and "discontinuit" in r.stderr, r.stderr
    assert not Path(str(out) + ".sigmf-meta").exists() and not Path(str(out) + ".sigmf-data").exists()
    # The same span ending before the gap is fine.
    r = clip_run(retune_recording, out, "--center", 10004000, "--rate", 48000, "--start-s", 2.1, "--seconds", 0.3)
    assert r.returncode == 0, r.stderr


def test_clip_refuses_to_run_past_a_retune_or_the_end(retune_recording, tmp_path):
    r = clip_run(retune_recording, tmp_path / "x", "--center", 10004000, "--rate", 48000, "--start-s", 0.8,
                 "--seconds", 0.5)
    assert r.returncode != 0 and "retune" in r.stderr, r.stderr
    r = clip_run(retune_recording, tmp_path / "y", "--center", 10004000, "--rate", 48000, "--start-s", 2.8,
                 "--seconds", 1.0)
    assert r.returncode != 0 and "end of the recording" in r.stderr, r.stderr


# --------------------------------------------------------------- guided script

# --------------------------------------------------------------- guided script
#
# Every run writes only inside its own pytest tmp_path (--results-dir / --bundle-dir), and
# nothing here deletes anything: the real defaults (<repo>/window1-results, /tmp/k2-window1-*)
# can hold hardware measurements and are only ever stat()ed, to prove they were not touched.

def _snapshot_meta(paths) -> dict:
    """{path: (size, mtime_ns)} for every file under the given roots, read-only."""
    out = {}
    for root in paths:
        root = Path(root)
        if root.is_file():
            st = root.stat()
            out[str(root)] = (st.st_size, st.st_mtime_ns)
        elif root.is_dir():
            for f in root.rglob("*"):
                if f.is_file():
                    st = f.stat()
                    out[str(f)] = (st.st_size, st.st_mtime_ns)
    return out


def _real_default_locations():
    return [ROOT / "window1-results", *Path("/tmp").glob("k2-window1-*")]


@pytest.fixture
def real_results_untouched():
    """Fails the test if anything under the real default result locations changed or vanished."""
    before = _snapshot_meta(_real_default_locations())
    yield
    after = _snapshot_meta(_real_default_locations())
    assert {k: v for k, v in after.items() if k in before} == before, "real Window 1 results were modified"
    assert set(after) == set(before), "a test wrote into the real Window 1 result locations"


def seed_existing(results: Path, bundles: Path) -> dict:
    """Plant 'earlier hardware results' where a new run will write, and hash them byte for byte."""
    rng = np.random.default_rng(7)
    old = results / "20261026-181353"
    (old / "rec").mkdir(parents=True)
    (old / "rec" / "wwv10.sigmf-data").write_bytes(rng.bytes(64 * 1024))
    (old / "rec" / "wwv10.sigmf-meta").write_text('{"global": {}, "captures": [], "annotations": []}\n')
    (old / "window1.jsonl").write_text('{"event":"operator","step":"real","note":"keep me"}\n')
    ob = bundles / "k2-window1-20261026-181353"
    ob.mkdir(parents=True)
    (ob / "airspyhf_wwv10_48k.sigmf-data").write_bytes(rng.bytes(32 * 1024))
    (bundles / "k2-window1-20261026-181353.tar.gz").write_bytes(rng.bytes(16 * 1024))
    (bundles / "k2-w1-extra.tar.gz").write_bytes(rng.bytes(8 * 1024))
    return _hash_tree(results, bundles)


def _hash_tree(*roots) -> dict:
    import hashlib
    return {str(f): hashlib.sha256(f.read_bytes()).hexdigest()
            for r in roots for f in sorted(Path(r).rglob("*")) if f.is_file()}


def assert_seed_intact(seed: dict) -> None:
    import hashlib
    for path, digest in seed.items():
        assert Path(path).is_file(), f"pre-existing result deleted: {path}"
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest, f"pre-existing result changed: {path}"


def run_window(tmp_path, probe_path=MOCK, extra_env=None, rec_s="3"):
    """Run the script with this test's own result/bundle parents; returns (result, run_dir, bundle)."""
    results, bundles = tmp_path / "results", tmp_path / "bundles"
    results.mkdir(exist_ok=True)
    bundles.mkdir(exist_ok=True)
    before_runs = {p.name for p in results.iterdir()}
    before_bundles = {p.name for p in bundles.glob("*.tar.gz")}
    env = dict(os.environ, W1_TEST="1", W1_PROBE=str(probe_path), W1_RATE_S="1", W1_LONG_S="1", W1_REC_S=rec_s,
               W1_UNPLUG_STREAM_S="4", W1_UNPLUG_ENV="MOCK_UNPLUG_AT_S=1.5", W1_UNPLUG_PROMPT_S="1",
               W1_REPLUG_WAIT_S="0", W1_CLIP_START="0.5", W1_CLIP_LEN="1", TMPDIR=str(tmp_path), **(extra_env or {}))
    r = subprocess.run(["bash", str(ROOT / "tools/window1/run_window1.sh"), "--results-dir", str(results),
                        "--bundle-dir", str(bundles)], input="\n" * 40, text=True, capture_output=True,
                       timeout=240, env=env, cwd=ROOT)
    new_runs = [results / n for n in {p.name for p in results.iterdir()} - before_runs]
    new_bundles = [bundles / n for n in {p.name for p in bundles.glob("*.tar.gz")} - before_bundles]
    assert len(new_runs) == 1 and len(new_bundles) == 1, (new_runs, new_bundles, r.stdout[-2000:])
    assert str(new_bundles[0]) in r.stdout                     # the path the operator is told to copy
    return r, new_runs[0], new_bundles[0]


def bundle_names(bundle: Path) -> str:
    return subprocess.run(["tar", "tzf", str(bundle)], capture_output=True, text=True, check=True).stdout


RESTORE = ("sudo systemctl start openwebrx", "sudo gpasswd -d k2dev plugdev")


def test_window_stops_when_no_device_can_be_opened(tmp_path, real_results_untouched):
    seed = seed_existing(tmp_path / "results", tmp_path / "bundles")
    r, run_dir, bundle = run_window(tmp_path, extra_env={"MOCK_BUSY": "1"})
    assert r.returncode != 0
    assert "STOPPED: no Airspy could be opened" in r.stdout
    assert "== Done" not in r.stdout
    tail = r.stdout[r.stdout.index("STOPPED"):]
    assert all(c in tail for c in RESTORE)                           # restore instructions shown last
    assert '"mode":"stream"' not in r.stdout and '"mode":"record"' not in r.stdout   # dependents skipped
    names = bundle_names(bundle)                                     # still diagnosable
    assert "FAILURES.txt" in names and "window1.jsonl" in names
    assert (run_dir / "window1.jsonl").is_file()
    assert_seed_intact(seed)                                         # failure path deleted nothing


def test_window_reports_a_failed_recording_and_skips_its_analysis(tmp_path, real_results_untouched):
    seed = seed_existing(tmp_path / "results", tmp_path / "bundles")
    wrapper = tmp_path / "probe.sh"
    wrapper.write_text(f'#!/bin/sh\ncase "$*" in *rec/wwv15*) echo "simulated disk failure" >&2; exit 5;; esac\n'
                       f'exec "{MOCK}" "$@"\n')
    wrapper.chmod(0o755)
    r, run_dir, bundle = run_window(tmp_path, probe_path=wrapper)
    assert r.returncode == 1, r.stdout[-3000:]
    assert "FINISHED WITH 1 FAILURE(S)" in r.stdout and "recording wwv15" in r.stdout
    assert "skipped: carrier analysis wwv15" in r.stdout
    assert "== Done" not in r.stdout
    assert "airspyhf_wwv10_48k.sigmf-meta" in bundle_names(bundle)  # independent work still delivered
    assert_seed_intact(seed)


def test_full_window_rehearsal(tmp_path, real_results_untouched):
    """Run tools/window1/run_window1.sh end to end against the mock, answering every prompt."""
    seed = seed_existing(tmp_path / "results", tmp_path / "bundles")
    r, run_dir, bundle = run_window(tmp_path)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    assert "== Done" in r.stdout and "FAILED" not in r.stdout
    assert "exit code: 4" in r.stdout                       # unplug detected
    names = bundle_names(bundle)
    for f in ("airspyhf_wwv10_48k.sigmf-meta", "airspyhf_ft8_40m_48k.sigmf-data", "window1.jsonl",
              "owrx-before.json", "owrx-after.json"):
        assert f in names
    log = [json.loads(x) for x in (run_dir / "window1.jsonl").read_text().splitlines() if x.startswith("{")]
    assert any(e.get("event") == "operator" for e in log)    # prompts and answers are recorded
    assert sum(1 for e in log if e.get("event") == "summary" and e.get("mode") == "record") == 7
    assert_seed_intact(seed)                                 # success path deleted nothing


def test_window_refuses_to_reuse_an_existing_run_directory(tmp_path, real_results_untouched):
    """Two runs in the same second would share a timestamp: the second must refuse, not overwrite."""
    results, bundles = tmp_path / "results", tmp_path / "bundles"
    stub = tmp_path / "date"
    stub.write_text("#!/bin/sh\necho 20261026-181353\n")      # pin the script's timestamp
    stub.chmod(0o755)
    seed = seed_existing(results, bundles)
    env = dict(os.environ, W1_TEST="1", W1_PROBE=str(MOCK), PATH=f"{tmp_path}:{os.environ['PATH']}")
    r = subprocess.run(["bash", str(ROOT / "tools/window1/run_window1.sh"), "--results-dir", str(results),
                        "--bundle-dir", str(bundles)], input="", text=True, capture_output=True, timeout=60,
                       env=env, cwd=ROOT)
    assert r.returncode == 1 and "Refusing to reuse" in r.stdout, r.stdout + r.stderr
    assert_seed_intact(seed)
    assert _hash_tree(results, bundles) == seed                  # and nothing new was written either


# --------------------------------------------------------------- clip timestamps across known gaps

def synthetic_recording(base: Path, captures, annotations, n=700_000, rate=192_000) -> Path:
    """cf32 noise with the given captures [(sample_start, freq, datetime)] and gap annotations."""
    rng = np.random.default_rng(1)
    x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64) * 0.01
    x.tofile(str(base) + ".sigmf-data")
    ann = []
    for at, lost in annotations:
        a = {"core:sample_start": at, "core:sample_count": 0, "core:label": "discontinuity"}
        if lost is not None:
            a["k2websdr:gap_samples"] = lost
        ann.append(a)
    meta = {"global": {"core:datatype": "cf32_le", "core:sample_rate": rate, "core:version": "1.2.6"},
            "captures": [{"core:sample_start": s, "core:frequency": f, "core:datetime": d} for s, f, d in captures],
            "annotations": ann}
    Path(str(base) + ".sigmf-meta").write_text(json.dumps(meta))
    return base


def clip_at(src, tmp_path, start_sample, seconds=0.25, center=10_001_000, rate=192_000):
    out = tmp_path / f"clip_{start_sample}"
    r = clip_run(src, out, "--center", center, "--rate", 48000, "--start-s", repr(start_sample / rate),
                 "--seconds", seconds)
    assert r.returncode == 0, r.stderr
    meta = json.loads(Path(str(out) + ".sigmf-meta").read_text())
    return meta["captures"][0].get("core:datetime"), meta["global"]["core:description"], json.loads(r.stdout)


T0 = "2026-10-26T22:00:00Z"


@pytest.mark.parametrize("annotations, start, expect", [
    ([], 230_400, "2026-10-26T22:00:01.200Z"),                                  # no gaps
    ([(96_000, 192_000)], 230_400, "2026-10-26T22:00:02.200Z"),                 # the reported reproduction
    ([(96_000, 192_000), (150_000, 9_600)], 230_400, "2026-10-26T22:00:02.250Z"),   # several earlier gaps
    ([(230_400, 19_200)], 230_400, "2026-10-26T22:00:01.300Z"),                 # gap right before the 1st sample
    ([(96_000, 192_000), (400_000, 50_000)], 230_400, "2026-10-26T22:00:02.200Z"),  # later gap ignored
])
def test_clip_datetime_includes_known_gaps_before_the_clip(tmp_path, annotations, start, expect):
    src = synthetic_recording(tmp_path / "src", [(0, 10_000_000, T0)], annotations)
    when, _, out = clip_at(src, tmp_path, start)
    assert when == expect and out["datetime"] == expect


def test_clip_in_a_later_capture_uses_that_captures_stamp_without_double_counting(tmp_path):
    # Capture 2 starts at sample 300000 with its own wall-clock stamp, which already includes the
    # 192000-sample gap of capture 1. Only the gap inside capture 2 (19200 samples) is added.
    src = synthetic_recording(tmp_path / "src",
                              [(0, 10_000_000, T0), (300_000, 15_000_000, "2026-10-26T22:00:10Z")],
                              [(96_000, 192_000), (350_000, 19_200)])
    when, _, _ = clip_at(src, tmp_path, 400_000, center=15_001_000)
    assert when == "2026-10-26T22:00:10.621Z"          # 10 s + (100000 + 19200) / 192000 = 10.6208 s


def test_unknown_length_gap_before_the_clip_omits_the_datetime_explicitly(tmp_path):
    src = synthetic_recording(tmp_path / "src", [(0, 10_000_000, T0)], [(96_000, None)])
    when, desc, out = clip_at(src, tmp_path, 230_400)
    assert when is None and out["datetime"] is None
    assert "unknown length" in out["datetime_note"] and "core:datetime omitted" in desc


def test_unknown_length_gap_in_an_earlier_capture_does_not_affect_a_later_one(tmp_path):
    src = synthetic_recording(tmp_path / "src",
                              [(0, 10_000_000, T0), (300_000, 15_000_000, "2026-10-26T22:00:10Z")],
                              [(96_000, None)])
    when, _, _ = clip_at(src, tmp_path, 396_000, center=15_001_000)
    assert when == "2026-10-26T22:00:10.500Z"


def test_clip_still_refuses_to_cross_a_known_or_unknown_gap(tmp_path):
    for lost in (19_200, None):
        src = synthetic_recording(tmp_path / f"src_{lost}", [(0, 10_000_000, T0)], [(250_000, lost)])
        r = clip_run(src, tmp_path / f"x_{lost}", "--center", 10_001_000, "--rate", 48000,
                     "--start-s", repr(230_400 / 192_000), "--seconds", 0.25)
        assert r.returncode != 0 and "discontinuit" in r.stderr, r.stderr
        assert ("an unknown number of" in r.stderr) == (lost is None)


@pytest.mark.parametrize("loss", [0, 3000])
def test_timing_log_measures_samples_lost_on_retune(tmp_path, loss):
    log = tmp_path / "timing.csv"
    code, out = probe("stream", "--serial", SERIAL, "--freq", "10005000", "--rate", "192000", "--seconds", "6",
                      "--retune", "15005000@2", "--retune", "10005000@4", "--timing-log", str(log),
                      env={"MOCK_RETUNE_LOSS": str(loss)})
    assert code == 0
    (tl,) = events(out, "timing_log")
    assert tl["ok"] and not tl["truncated"] and tl["callbacks"] > 1000
    res = tool("timing.py", log, "--rate", 192000)
    assert len(res) == 2
    for r in res:
        assert abs(r["lost_samples"] - loss) < 150, res               # < 1 ms at 192k
        assert r["library_dropped_in_span"] == 0                       # the mock hides it, like a silent loss
        assert abs(r["rate_before_hz"] - 192000) < 200 and abs(r["rate_after_hz"] - 192000) < 200
