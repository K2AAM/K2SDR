"""ksdr-worker --source airspyhf against the mock libairspyhf (M1 step 4a). No hardware.

The worker is linked against worker/probe/mock_airspyhf.c: a synthetic Airspy HF+ (tones at
10.005 and 15.005 MHz, real-time paced 1024-sample blocks, the rate list the real device
reported) with scripted faults modelled on Window 1/1b: library drops, unplug with stop/close
hanging afterwards, silent retune loss and settling junk, and driver errors.
K2_WORKER_AIRSPYHF_MOCK_BIN selects another build (CI also runs this under ASan/UBSan).
"""

from __future__ import annotations

import os
import struct
import time
from pathlib import Path

import numpy as np
import pytest

from k2websdr.protocol import worker_ctl as wc
from k2websdr.protocol.frame import FrameFlags, FrameKind, SampleFormat
from k2websdr.replay.worker_argv import HwGain, WorkerIdentity, airspyhf_argv

from worker_harness import ROOT, WorkerProc

F = FrameFlags
MOCK_BIN = Path(os.environ.get("K2_WORKER_AIRSPYHF_MOCK_BIN", ROOT / "build/worker/ksdr-worker-airspyhf-mock"))
SERIAL = 0xA1B2C3D4E5F60718
RATE = 384000
CENTER = 10_000_000          # mock tone at 10.005 MHz -> +5000 Hz

if not MOCK_BIN.exists():
    if os.environ.get("K2_REQUIRE_PROBE") == "1":
        raise RuntimeError(f"{MOCK_BIN} missing; build with libairspyhf-dev installed")
    pytest.skip("airspyhf mock worker not built (libairspyhf-dev not installed)", allow_module_level=True)


@pytest.fixture
def ident(tmp_path) -> WorkerIdentity:
    d = tmp_path / "locks"
    d.mkdir()
    return WorkerIdentity(device_id="airspyhf-a1b2", lock_dir=d, receiver_idx=2, stream_id=5)


@pytest.fixture
def calls(tmp_path) -> Path:
    return tmp_path / "calls.log"


def live(ident, calls, *, gain=HwGain(), rate=RATE, center=CENTER, serial=SERIAL, extra=(), env=None,
         expect_running=True, **kw):
    def argv(ctl_fd, data_fd):
        return airspyhf_argv(MOCK_BIN, ident, serial=serial, rate_hz=rate, center_hz=center, ctl_fd=ctl_fd,
                             data_fd=data_fd, gain=gain, **kw) + list(extra)
    e = {**os.environ, "MOCK_CALL_LOG": str(calls), "MOCK_SETTLE_MS": "0", **(env or {})}
    return WorkerProc(None, ident, argv_fn=argv, env=e, expect_running=expect_running)


def call_log(path: Path) -> list[tuple[str, int]]:
    if not path.exists():
        return []
    return [(c, int(v)) for c, v in (line.split() for line in path.read_text().splitlines())]


def samples(frames) -> np.ndarray:
    return np.concatenate([np.frombuffer(f.payload, np.complex64) for f in frames])


def tone_hz(x: np.ndarray, rate=RATE) -> float:
    spec = np.abs(np.fft.fft(x * np.hanning(len(x))))
    return float(np.fft.fftfreq(len(x), 1 / rate)[int(np.argmax(spec))])


def purity(x: np.ndarray) -> float:
    """Share of the block's power in its strongest +-3 bins (a clean tone is near 1)."""
    p = np.abs(np.fft.fft(x * np.hanning(len(x)))) ** 2
    k = int(np.argmax(p))
    return float(p[np.arange(k - 3, k + 4) % len(p)].sum() / p.sum())


def expect_ack(w, seq, result=wc.Result.OK):
    hdr, p = w.read_ctl()
    assert hdr.msg_type is wc.MsgType.ACK and hdr.cmd_seq == seq, hdr
    assert wc.unpack_result(wc.MsgType.ACK, p) is result


def read_status(w, timeout=5.0) -> wc.Status:
    hdr, p = w.read_ctl(timeout)
    assert hdr.msg_type is wc.MsgType.STATUS, hdr
    return wc.Status.unpack_payload(p)


# --------------------------------------------------------------------------------------- open

def test_opens_by_serial_and_sets_every_control_explicitly(ident, calls):
    gain = HwGain(hf_agc=False, hf_agc_threshold_high=True, hf_lna=False, hf_att=3)
    w = live(ident, calls, gain=gain)
    try:
        w.read_frame()
    finally:
        w.close()
    assert call_log(calls)[:8] == [("set_samplerate", RATE), ("set_freq", CENTER), ("set_lib_dsp", 1),
                                   ("set_hf_agc", 0), ("set_hf_agc_threshold", 1), ("set_hf_lna", 0),
                                   ("set_hf_att", 3), ("start", 0)]


def test_approved_defaults_are_what_the_argv_sends(ident, calls):
    w = live(ident, calls)
    w.close()
    assert ("set_hf_agc", 1) in call_log(calls) and ("set_hf_agc_threshold", 0) in call_log(calls)
    assert ("set_hf_lna", 1) in call_log(calls) and ("set_hf_att", 0) in call_log(calls)


def test_streams_cf32_frames_contiguously_with_the_tone_where_it_belongs(ident, calls):
    w = live(ident, calls)
    try:
        frames = [w.read_frame() for _ in range(12)]
    finally:
        w.close()
    h0 = frames[0].header
    assert all(f.header.kind is FrameKind.SAMPLES and f.header.format is SampleFormat.CF32 for f in frames)
    assert {(f.header.sample_rate_hz, f.header.freq_origin_hz, f.header.capture_gen, f.header.stream_id)
            for f in frames} == {(RATE, CENTER, 1, 5)}
    assert F.NEW_GEN in h0.flags and h0.first_sample_index == 0
    idx = [f.header.first_sample_index for f in frames]
    assert idx == [4096 * i for i in range(12)]                        # whole frames, no gaps
    assert not any(F.DISCONTINUITY in f.header.flags for f in frames[1:])
    assert abs(tone_hz(samples(frames)) - 5000) < RATE / 4096


@pytest.mark.parametrize("env, result, code", [
    ({"MOCK_BUSY": "1"}, wc.Result.E_BUSY, 75),           # listed, cannot be opened: someone else has it
    ({"MOCK_DEVICES": "0"}, wc.Result.E_DEVICE, 69),       # not connected
])
def test_busy_and_absent_devices_fail_fast_and_say_which(ident, calls, env, result, code):
    w = live(ident, calls, env=env, expect_running=False)
    try:
        st = read_status(w)
        assert (st.state, st.result) == (wc.WorkerState.FAILED, result)
        assert w.wait(5) == code
    finally:
        w.close()


def test_wrong_serial_is_not_opened(ident, calls):
    w = live(ident, calls, serial=0x1234, expect_running=False)
    try:
        assert read_status(w).result is wc.Result.E_DEVICE and w.wait(5) == 69
    finally:
        w.close()


def test_rate_the_device_does_not_offer_is_refused(ident, calls):
    w = live(ident, calls, rate=100000, expect_running=False)
    try:
        assert read_status(w).result is wc.Result.E_UNSUPPORTED and w.wait(5) == 64
    finally:
        w.close()
    assert ("start", 0) not in call_log(calls)


def test_device_lock_is_taken_before_the_device_is_touched(ident, calls):
    first = live(ident, calls)
    try:
        before = len(call_log(calls))
        second = live(ident, calls, expect_running=False)
        try:
            assert read_status(second).result is wc.Result.E_BUSY and second.wait(5) == 75
        finally:
            second.close()
        assert len(call_log(calls)) == before                          # the second never opened it
    finally:
        first.close()


@pytest.mark.parametrize("bad", [["--file", "x.sigmf-data"], ["--origin-hz", "1"], ["--paced"],
                                 ["--start-after-ping"]])
def test_replay_only_arguments_are_refused(ident, calls, bad):
    w = live(ident, calls, extra=bad, expect_running=False)
    try:
        assert w.wait(5) == 64
    finally:
        w.close()


# -------------------------------------------------------------------------------- retune / rate

@pytest.mark.parametrize("env", [
    {"MOCK_SETTLE_MS": "4"},                            # 4 ms of wideband junk after set_freq
    {"MOCK_SETTLE_MS": "4", "MOCK_RETUNE_LOSS": "1490"},   # plus the silent loss measured in Window 1b
])
def test_retune_starts_a_generation_and_discards_the_blocks_around_the_step(ident, calls, env):
    # Two discarded 1024-sample blocks (5.3 ms at 384k) cover the junk; without the discard the
    # first frame of the new generation would carry it.
    w = live(ident, calls, env=env)
    try:
        old = [w.read_frame() for _ in range(4)]
        seq = w.set_capture(15_002_000)                                 # tone 2 (15.005 MHz) -> +3000
        expect_ack(w, seq)
        hdr, p = w.read_ctl()
        assert hdr.msg_type is wc.MsgType.APPLIED and hdr.cmd_seq == seq
        ap = wc.Applied.unpack_payload(p)
        assert (ap.capture_gen, ap.center_hz, ap.sample_rate_hz, ap.verified) == (2, 15_002_000, RATE, 0)
        frames = [w.read_frame() for _ in range(30)]
    finally:
        w.close()
    g1 = old + [f for f in frames if f.header.capture_gen == 1]
    g2 = [f for f in frames if f.header.capture_gen == 2]
    assert len(g2) >= 10
    assert all(f.header.freq_origin_hz == CENTER for f in g1) and abs(tone_hz(samples(g1[-4:])) - 5000) < 200
    assert g2[0].header.flags & (F.NEW_GEN | F.DISCONTINUITY) == F.NEW_GEN | F.DISCONTINUITY
    assert [f.header.first_sample_index for f in g2] == [4096 * i for i in range(len(g2))]
    assert all(f.header.freq_origin_hz == 15_002_000 for f in g2)
    assert all(abs(tone_hz(np.frombuffer(f.payload, np.complex64)) - 3000) < 200 for f in g2)
    assert min(purity(np.frombuffer(f.payload, np.complex64)) for f in g2) > 0.9   # no settling junk
    assert ("set_freq", 15_002_000) in call_log(calls)


def test_rate_change_is_a_stop_set_start_and_a_new_generation(ident, calls):
    w = live(ident, calls)
    try:
        w.read_frame()
        seq = w.set_capture(rate_hz=192000)
        expect_ack(w, seq)
        hdr, p = w.read_ctl()
        assert wc.Applied.unpack_payload(p).sample_rate_hz == 192000
        frames = [w.read_frame() for _ in range(20)]
    finally:
        w.close()
    g2 = [f for f in frames if f.header.capture_gen == 2]
    assert g2 and all(f.header.sample_rate_hz == 192000 for f in g2)
    assert abs(tone_hz(samples(g2), 192000) - 5000) < 100
    log = call_log(calls)
    i = log.index(("set_samplerate", 192000))
    assert log[i - 1] == ("stop", 0) and log[i + 1] == ("start", 0)


def test_set_capture_values_are_checked_before_the_device_is_touched(ident, calls):
    w = live(ident, calls)
    try:
        expect_ack(w, w.set_capture(rate_hz=100000), wc.Result.E_UNSUPPORTED)       # not in the list
        expect_ack(w, w.set_capture(5_000_000_000), wc.Result.E_OUT_OF_RANGE)        # beyond u32 Hz
        raw = wc.SetCapture(1, None).pack_payload()[:0] + struct.pack("<qII", -5, 0, 1)
        expect_ack(w, w.send(wc.MsgType.SET_CAPTURE, raw), wc.Result.E_OUT_OF_RANGE)
        assert w.read_frame().header.capture_gen == 1                                # still streaming
    finally:
        w.close()
    assert [c for c in call_log(calls) if c[0] == "set_freq"] == [("set_freq", CENTER)]


def test_driver_refusing_a_retune_fails_the_command_and_restarts_cleanly(ident, calls):
    w = live(ident, calls, env={"MOCK_FAIL": "set_freq", "MOCK_FAIL_SKIP": "1"})
    try:
        w.read_frame()
        seq = w.set_capture(15_000_000)
        expect_ack(w, seq)
        hdr, p = w.read_ctl()
        assert hdr.msg_type is wc.MsgType.FAILED and wc.unpack_result(wc.MsgType.FAILED, p) is wc.Result.E_DEVICE
        st = read_status(w)
        assert (st.state, st.result) == (wc.WorkerState.FAILED, wc.Result.E_DEVICE)
        assert w.wait(5) == 69                     # device state unknown: the service restarts it
    finally:
        w.close()


# ------------------------------------------------------------------------------------- SET_HW

def set_hw(w, payload: bytes):
    seq = w.send(wc.MsgType.SET_HW, payload)
    hdr, p = w.read_ctl()
    assert hdr.msg_type is wc.MsgType.ACK and hdr.cmd_seq == seq
    res = wc.unpack_result(wc.MsgType.ACK, p)
    if res is not wc.Result.OK:
        return res, None
    hdr, p = w.read_ctl()
    assert hdr.cmd_seq == seq
    return res, (wc.HwApplied.unpack_payload(p) if hdr.msg_type is wc.MsgType.HW_APPLIED else hdr)


def test_set_hw_changes_only_what_it_names_and_reports_every_value(ident, calls):
    w = live(ident, calls)
    try:
        res, ap = set_hw(w, wc.SetHw(hf_agc=False, hf_att=4).pack_payload())
        assert res is wc.Result.OK
        assert (ap.capture_gen, ap.hf_agc, ap.hf_agc_threshold_high, ap.hf_lna, ap.hf_att, ap.verified) == (
            1, False, False, True, 4, 0)
        res, ap = set_hw(w, wc.SetHw(hf_lna=False).pack_payload())
        assert (ap.hf_agc, ap.hf_lna, ap.hf_att) == (False, False, 4)
        assert w.read_frame().header.capture_gen == 1                  # gain is not a new generation
    finally:
        w.close()
    tail = [c for c in call_log(calls) if c[0].startswith("set_hf")][-4:]   # every control, every time
    assert tail == [("set_hf_agc", 0), ("set_hf_agc_threshold", 0), ("set_hf_lna", 0), ("set_hf_att", 4)]


@pytest.mark.parametrize("payload, result", [
    (struct.pack("<IBBBBQ", 0x8, 0, 0, 0, 9, 0), wc.Result.E_OUT_OF_RANGE),       # att 9
    (struct.pack("<IBBBBQ", 0x1, 2, 0, 0, 0, 0), wc.Result.E_OUT_OF_RANGE),       # agc 2
    (struct.pack("<IBBBBQ", 0x0, 0, 0, 0, 0, 0), wc.Result.E_BAD_MESSAGE),        # nothing to change
    (struct.pack("<IBBBBQ", 0x10, 0, 0, 0, 0, 0), wc.Result.E_BAD_MESSAGE),       # unknown flag
    (struct.pack("<IBBBBQ", 0x8, 0, 0, 0, 1, 1), wc.Result.E_BAD_MESSAGE),        # reserved
])
def test_bad_set_hw_is_refused_and_the_device_is_untouched(ident, calls, payload, result):
    w = live(ident, calls)
    try:
        before = call_log(calls)
        assert set_hw(w, payload) == (result, None)
        w.read_frame()
    finally:
        w.close()
    assert [c for c in call_log(calls) if c[0].startswith("set_hf")] == \
        [c for c in before if c[0].startswith("set_hf")]


def test_replay_sources_have_no_hardware_gain(tmp_path):
    from k2websdr.replay import sigmf
    d = tmp_path / "l"
    d.mkdir()
    rec = sigmf.load(ROOT / "fixtures/iq/two_tone_ci16.sigmf-meta", require_sha512=True)
    w = WorkerProc(rec, WorkerIdentity(device_id="r", lock_dir=d, receiver_idx=0))
    try:
        assert set_hw(w, wc.SetHw(hf_att=1).pack_payload()) == (wc.Result.E_UNSUPPORTED, None)
    finally:
        w.close()


def test_driver_refusing_a_gain_change_is_fatal(ident, calls):
    w = live(ident, calls, env={"MOCK_FAIL": "set_hf_att", "MOCK_FAIL_SKIP": "1"})
    try:
        res, hdr = set_hw(w, wc.SetHw(hf_att=2).pack_payload())
        assert res is wc.Result.OK and hdr.msg_type is wc.MsgType.FAILED
        assert read_status(w).result is wc.Result.E_DEVICE and w.wait(5) == 69
    finally:
        w.close()
    assert ("stop", 0) not in call_log(calls) and ("close", 0) not in call_log(calls)


# ----------------------------------------------------------------------------- losses and faults

def test_library_drops_become_a_known_gap_at_a_frame_boundary(ident, calls):
    w = live(ident, calls, env={"MOCK_DROP_AT_S": "0.2", "MOCK_DROP_SAMPLES": "1000"})
    try:
        frames = [w.read_frame() for _ in range(40)]
    finally:
        w.close()
    gaps = [i for i, f in enumerate(frames) if F.DISCONTINUITY in f.header.flags and i > 0]
    assert len(gaps) == 1
    g = frames[gaps[0]].header
    assert g.flags & (F.GAP_KNOWN | F.OVERFLOW) == F.GAP_KNOWN | F.OVERFLOW and g.gap_samples == 1000
    prev = frames[gaps[0] - 1].header
    assert prev.first_sample_index + prev.sample_count == g.first_sample_index   # index counts delivered
    assert prev.sample_count % 1024 == 0 and prev.sample_count < 4096           # staged samples went first


def test_unplug_reports_failure_and_exits_without_stop_or_close(ident, calls):
    # Window 1b H7: after the cable is pulled, airspyhf_stop/close never return.
    w = live(ident, calls, env={"MOCK_UNPLUG_AT_S": "0.3", "MOCK_STOP_HANG": "1"})
    t0 = time.monotonic()
    try:
        while True:
            hdr, p = w.read_ctl(5)
            if hdr.msg_type is wc.MsgType.STATUS:
                break
        st = wc.Status.unpack_payload(p)
        assert (st.state, st.result) == (wc.WorkerState.FAILED, wc.Result.E_DEVICE)
        assert w.wait(5) == 69
    finally:
        w.close()
    assert time.monotonic() - t0 < 4
    assert ("stop", 0) not in call_log(calls) and ("close", 0) not in call_log(calls)


def test_silent_stall_is_detected_as_device_loss(ident, calls):
    w = live(ident, calls, env={"MOCK_STALL_AT_S": "0.3"})
    t0 = time.monotonic()
    try:
        st = read_status(w, timeout=5)
        assert (st.state, st.result) == (wc.WorkerState.FAILED, wc.Result.E_DEVICE)
        assert w.wait(5) == 69
        assert 1.0 <= time.monotonic() - t0 < 4                        # 1 s without a block
    finally:
        w.close()


def test_stop_releases_the_device(ident, calls):
    w = live(ident, calls)
    try:
        w.read_frame()
        seq = w.send(wc.MsgType.STOP)
        expect_ack(w, seq)
        assert w.wait(5) == 0
    finally:
        w.close()
    assert call_log(calls)[-2:] == [("stop", 0), ("close", 0)]


def test_a_hanging_stop_is_bounded_by_the_watchdog(ident, calls):
    w = live(ident, calls, env={"MOCK_STOP_HANG": "2"})
    try:
        w.read_frame()
        t0 = time.monotonic()
        expect_ack(w, w.send(wc.MsgType.STOP))
        assert w.wait(8) == 71
        assert time.monotonic() - t0 < 6
    finally:
        w.close()


def test_slow_consumer_drops_whole_frames_with_known_gaps_and_bounded_memory(ident, calls):
    w = live(ident, calls, rate=912000)                                # ~7.3 MB/s of CF32
    try:
        w.read_frame()
        time.sleep(3.0)                                                # stop reading
        rss_kb = int(next(line.split()[1] for line in Path(f"/proc/{w.proc.pid}/status").read_text().splitlines()
                          if line.startswith("VmRSS:")))
        frames = [w.read_frame() for _ in range(600)]
    finally:
        w.close()
    assert rss_kb < 80_000                                             # backlog bounded (8 MB) + ring
    gapped = [f.header for f in frames if F.DISCONTINUITY in f.header.flags]
    assert gapped and all(F.GAP_KNOWN in h.flags and F.OVERFLOW not in h.flags for h in gapped)
    for a, b in zip(frames, frames[1:]):                               # delivered index stays contiguous
        assert a.header.first_sample_index + a.header.sample_count == b.header.first_sample_index


# --------------------------------------------------------------------------------------- DSP

def test_spectrum_and_a_listener_channel_on_the_live_source(ident, calls):
    w = live(ident, calls, spectrum_fft=2048, spectrum_fps=10, iq_frames=False)
    try:
        res, ap = w.channel(wc.MsgType.CHANNEL_OPEN, wc.Channel(1, wc.Mode.USB, wc.Agc.MEDIUM, 4000))
        assert res is wc.Result.OK
        frames = [w.read_frame() for _ in range(80)]
    finally:
        w.close()
    spec = [f for f in frames if f.header.kind is FrameKind.SPECTRUM]
    au = [f for f in frames if f.header.kind is FrameKind.AUDIO]
    assert spec and au and not [f for f in frames if f.header.kind is FrameKind.SAMPLES]
    bins = np.mean([np.frombuffer(f.payload, np.uint8) for f in spec[1:]], axis=0)
    assert abs(int(np.argmax(bins)) - (1024 + round(5000 * 2048 / RATE))) <= 1
    a = np.concatenate([np.frombuffer(f.payload, "<i2") for f in au[3:]]).astype(float)
    assert abs(tone_hz(a, 12000) - 1000) < 30                          # 5000 - 4000 in USB
    assert {f.header.freq_origin_hz for f in au} == {CENTER + 4000}


def test_async_client_set_hw_and_set_capture_against_the_live_worker(ident, calls, monkeypatch):
    """The control service's WorkerProcess speaks SET_HW (HW_APPLIED) and live SET_CAPTURE."""
    import asyncio

    from k2websdr.devices.worker_client import Callbacks, WorkerProcess, WorkerRejected

    monkeypatch.setenv("MOCK_CALL_LOG", str(calls))
    monkeypatch.setenv("MOCK_SETTLE_MS", "0")

    async def run():
        frames = []
        wp = WorkerProcess(lambda c, d, g: airspyhf_argv(MOCK_BIN, ident, serial=SERIAL, rate_hz=RATE,
                                                         center_hz=CENTER, ctl_fd=c, data_fd=d, capture_gen_start=g),
                           Callbacks(on_frame=lambda h, p: frames.append(h)))
        await wp.start(7)
        try:
            ap = await wp.set_hw(wc.SetHw(hf_agc=False, hf_att=6), 5)
            assert (ap.capture_gen, ap.hf_agc, ap.hf_att) == (7, False, 6)
            try:
                await wp.set_hw(wc.SetHw(hf_att=8, hf_lna=True), 5)
            except WorkerRejected:
                raise AssertionError("valid SET_HW refused") from None
            cap = await wp.set_capture(7_100_000, None, 5)
            assert (cap.capture_gen, cap.center_hz, cap.sample_rate_hz) == (8, 7_100_000, RATE)
            for _ in range(100):
                if any(h.capture_gen == 8 for h in frames):
                    break
                await asyncio.sleep(0.02)
            assert any(h.capture_gen == 8 and h.freq_origin_hz == 7_100_000 for h in frames)
        finally:
            await wp.stop(5)

    asyncio.run(asyncio.wait_for(run(), 30))
    assert call_log(calls)[-2:] == [("stop", 0), ("close", 0)]
