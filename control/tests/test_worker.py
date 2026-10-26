"""ksdr-worker (M0 replay): A5 single device owner, A6 deterministic replay,
A8 replay half (discontinuity flags), and the worker control lifecycle.

Requires the worker binary (make worker). K2_WORKER_BIN selects another build,
e.g. the sanitizer build in CI.
"""

from __future__ import annotations

import dataclasses
import os
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from k2websdr.protocol import worker_ctl as wc
from k2websdr.protocol.frame import FrameFlags, FrameKind, SampleFormat
from k2websdr.replay import sigmf
from k2websdr.replay.worker_argv import WorkerIdentity, replay_argv

from worker_harness import WORKER_BIN, Frame, WorkerProc

ROOT = Path(__file__).resolve().parents[2]
IQ = ROOT / "fixtures/iq"
F = FrameFlags

@pytest.fixture(scope="module")
def recs() -> dict[str, sigmf.Recording]:
    return {n: sigmf.load(IQ / f"{n}.sigmf-meta", require_sha512=True)
            for n in ("two_tone_ci16", "tone_ri16", "gap_ci16")}


@pytest.fixture
def ident(tmp_path) -> WorkerIdentity:
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    return WorkerIdentity(device_id="replay-test", lock_dir=lock_dir, receiver_idx=3, stream_id=9)


def test_worker_binary_exists():
    assert WORKER_BIN.is_file(), f"{WORKER_BIN} missing; run: make worker"


def payload_of(frames: list[Frame]) -> bytes:
    return b"".join(f.payload for f in frames)


def run_to_eos(rec, ident, **kw) -> list[Frame]:
    with WorkerProc(rec, ident, **kw) as w:
        frames = w.read_until_eos()
        return frames


# ----------------------------------------------------------- A6: determinism

@pytest.mark.parametrize("name", ["two_tone_ci16", "tone_ri16", "gap_ci16"])
def test_replay_delivers_exact_file_bytes_with_consistent_headers(recs, ident, name):
    rec = recs[name]
    frames = run_to_eos(rec, ident, frame_samples=1000)
    *data, eos = frames
    assert payload_of(data) == rec.data_path.read_bytes()
    assert eos.header.kind is FrameKind.END_OF_STREAM and eos.header.sample_count == 0
    expected_index = 0
    for i, f in enumerate(frames):
        h = f.header
        assert h.seq == i
        assert h.capture_gen == 1
        assert h.format is rec.format
        assert h.sample_rate_hz == rec.sample_rate_hz
        assert h.freq_origin_hz == rec.freq_origin_hz
        assert (h.receiver_idx, h.stream_id) == (3, 9)
        assert h.first_sample_index == expected_index
        expected_index += h.sample_count
    assert F.NEW_GEN in frames[0].header.flags
    assert all(F.NEW_GEN not in f.header.flags for f in frames[1:])
    assert expected_index == rec.sample_count


def test_two_runs_are_identical_except_host_time(recs, ident):
    rec = recs["gap_ci16"]
    a = run_to_eos(rec, ident, frame_samples=777)
    b = run_to_eos(rec, ident, frame_samples=777)
    assert [f.header.determinism_key() for f in a] == [f.header.determinism_key() for f in b]
    assert [f.payload for f in a] == [f.payload for f in b]


def test_paced_mode_delivers_the_same_samples_at_real_time_rate(recs, ident):
    rec = recs["two_tone_ci16"]                       # 12000 samples @ 48 kS/s = 250 ms
    unpaced = run_to_eos(rec, ident, frame_samples=1200)
    t0 = time.monotonic()
    paced = run_to_eos(rec, ident, frame_samples=1200, paced=True)
    elapsed = time.monotonic() - t0
    assert payload_of(paced) == payload_of(unpaced)
    assert [f.header.determinism_key() for f in paced] == [f.header.determinism_key() for f in unpaced]
    assert elapsed >= 0.22, f"paced replay finished too fast: {elapsed:.3f}s"
    assert elapsed < 2.0


def test_host_time_is_populated_and_monotonic(recs, ident):
    frames = run_to_eos(recs["two_tone_ci16"], ident, frame_samples=3000)
    times = [f.header.host_time_ns for f in frames]
    assert all(t > 1_700_000_000 * 10**9 for t in times)
    assert times == sorted(times)


def test_capture_gen_start_carries_generation_across_restarts(recs, ident):
    rec = recs["two_tone_ci16"]
    with WorkerProc(rec, ident, frame_samples=4000, capture_gen_start=41) as w:
        frames = w.read_until_eos()
        assert {f.header.capture_gen for f in frames} == {41}
        assert F.NEW_GEN in frames[0].header.flags
        w.set_capture(center_hz=rec.freq_origin_hz)
        w.read_ctl()
        assert wc.Applied.unpack_payload(w.read_ctl()[1]).capture_gen == 42


# ----------------------------------------------- A8: replay discontinuity flags

def test_gap_fixture_flags_land_on_annotated_samples(recs, ident):
    rec = recs["gap_ci16"]
    frames = run_to_eos(rec, ident, frame_samples=1000)
    by_start = {f.header.first_sample_index: f.header for f in frames if f.header.sample_count}
    # Frames are split exactly at the annotated samples.
    assert 4_800 in by_start and 7_200 in by_start
    known, unknown = by_start[4_800], by_start[7_200]
    assert known.flags == F.DISCONTINUITY | F.GAP_KNOWN and known.gap_samples == 2_412
    assert unknown.flags == F.DISCONTINUITY and unknown.gap_samples == 0
    others = [h for i, h in by_start.items() if i not in (0, 4_800, 7_200)]
    assert others and all(h.flags == F(0) for h in others)
    assert by_start[0].flags == F.NEW_GEN


def test_loop_wraps_with_discontinuity_in_same_generation(recs, ident):
    rec = recs["tone_ri16"]                           # 16384 samples
    with WorkerProc(rec, ident, frame_samples=4096, loop=True) as w:
        frames = [w.read_frame() for _ in range(6)]   # 4 frames per pass
    assert [f.header.first_sample_index for f in frames] == [0, 4096, 8192, 12288, 16384, 20480]
    assert frames[4].header.flags == F.DISCONTINUITY
    assert frames[4].payload == frames[0].payload
    assert {f.header.capture_gen for f in frames} == {1}


# ------------------------------------------------------- A5: single owner

def test_second_worker_on_same_device_is_refused_and_first_keeps_streaming(recs, ident):
    rec = recs["two_tone_ci16"]
    with WorkerProc(rec, ident, frame_samples=1200, paced=True, loop=True) as a:
        a.read_frame()
        with WorkerProc(rec, ident, expect_running=False) as b:
            hdr, payload = b.read_ctl()
            st = wc.Status.unpack_payload(payload)
            assert (hdr.msg_type, st.state, st.result) == (wc.MsgType.STATUS, wc.WorkerState.FAILED, wc.Result.E_BUSY)
            assert b.wait() == 75
            assert "busy" in b.stderr()
        # A is unaffected.
        f1, f2 = a.read_frame(), a.read_frame()
        assert f2.header.seq == f1.header.seq + 1
        assert (ident.lock_dir / "replay-test.lock").read_text().strip() == str(a.proc.pid)


def test_different_devices_run_concurrently(recs, ident):
    other = WorkerIdentity(device_id="replay-other", lock_dir=ident.lock_dir, receiver_idx=4)
    with WorkerProc(recs["two_tone_ci16"], ident, paced=True, loop=True) as a, \
         WorkerProc(recs["tone_ri16"], other, paced=True, loop=True) as b:
        assert a.read_frame().header.receiver_idx == 3
        assert b.read_frame().header.receiver_idx == 4


@pytest.mark.parametrize("how", ["stop", "sigkill"])
def test_lock_is_released_when_owner_exits(recs, ident, how):
    rec = recs["two_tone_ci16"]
    a = WorkerProc(rec, ident, paced=True, loop=True)
    try:
        a.read_frame()
        if how == "stop":
            a.send(wc.MsgType.STOP)
            assert a.wait() == 0
        else:
            a.proc.send_signal(signal.SIGKILL)
            a.wait()
    finally:
        a.close()
    with WorkerProc(rec, ident) as c:                 # acquires: RUNNING asserted by the harness
        assert c.read_frame().header.seq == 0


# ------------------------------------------------------- control lifecycle

def test_ping_gets_pong_and_no_ack(recs, ident):
    with WorkerProc(recs["two_tone_ci16"], ident, paced=True, loop=True) as w:
        seq = w.send(wc.MsgType.PING)
        hdr, payload = w.read_ctl()
        assert (hdr.msg_type, hdr.cmd_seq, payload) == (wc.MsgType.PONG, seq, b"")
        seq2 = w.send(wc.MsgType.PING)
        assert w.read_ctl()[0].cmd_seq == seq2


def _gen_sequence(frames: list[Frame]) -> list[int]:
    return [f.header.capture_gen for f in frames]


def test_set_capture_same_values_is_a_reacquisition(recs, ident):
    rec = recs["two_tone_ci16"]
    with WorkerProc(rec, ident, frame_samples=1000, paced=True, loop=True) as w:
        before = [w.read_frame() for _ in range(3)]
        seq = w.set_capture(center_hz=rec.freq_origin_hz, rate_hz=rec.sample_rate_hz)
        ack_h, ack_p = w.read_ctl()
        assert (ack_h.msg_type, ack_h.cmd_seq) == (wc.MsgType.ACK, seq)
        assert wc.unpack_result(wc.MsgType.ACK, ack_p) is wc.Result.OK
        ap_h, ap_p = w.read_ctl()
        assert (ap_h.msg_type, ap_h.cmd_seq) == (wc.MsgType.APPLIED, seq)
        applied = wc.Applied.unpack_payload(ap_p)
        assert applied == wc.Applied(capture_gen=2, center_hz=7_074_000, sample_rate_hz=48_000, verified=0)
        after = [w.read_frame() for _ in range(6)]
    frames = before + after
    gens = _gen_sequence(frames)
    assert gens == sorted(gens), "generations never interleave"
    first_new = next(f for f in frames if f.header.capture_gen == 2)
    assert first_new.header.flags == F.NEW_GEN | F.DISCONTINUITY
    assert first_new.header.first_sample_index == 0
    assert first_new.payload == before[0].payload        # playback restarted at file sample 0
    seqs = [f.header.seq for f in frames]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), "seq never resets"


@pytest.mark.parametrize("name,center,rate", [
    ("two_tone_ci16", 14_074_000, None),     # replay cannot relabel to another frequency
    ("two_tone_ci16", None, 96_000),         # nor another rate
    ("tone_ri16", 0, None),                  # real data has no tunable center
])
def test_set_capture_unsupported_values_are_rejected_without_new_generation(recs, ident, name, center, rate):
    with WorkerProc(recs[name], ident, frame_samples=1000, paced=True, loop=True) as w:
        w.read_frame()
        seq = w.set_capture(center_hz=center, rate_hz=rate)
        hdr, p = w.read_ctl()
        assert (hdr.msg_type, hdr.cmd_seq) == (wc.MsgType.ACK, seq)
        assert wc.unpack_result(wc.MsgType.ACK, p) is wc.Result.E_UNSUPPORTED
        assert w.send(wc.MsgType.PING) and w.read_ctl()[0].msg_type is wc.MsgType.PONG   # no APPLIED in between
        assert {w.read_frame().header.capture_gen for _ in range(3)} == {1}


def test_real_recording_accepts_its_own_rate(recs, ident):
    with WorkerProc(recs["tone_ri16"], ident, frame_samples=4096, paced=True, loop=True) as w:
        w.read_frame()
        seq = w.set_capture(rate_hz=64_000)
        assert wc.unpack_result(wc.MsgType.ACK, w.read_ctl()[1]) is wc.Result.OK
        ap_h, ap_p = w.read_ctl()
        assert ap_h.cmd_seq == seq and wc.Applied.unpack_payload(ap_p).center_hz == 0


def test_set_capture_after_eos_restarts_playback(recs, ident):
    rec = recs["two_tone_ci16"]
    with WorkerProc(rec, ident, frame_samples=4000) as w:
        first = w.read_until_eos()
        w.set_capture(center_hz=rec.freq_origin_hz)
        w.read_ctl(); w.read_ctl()                    # ACK, APPLIED
        second = w.read_until_eos()
    assert payload_of(first) == payload_of(second)
    assert {f.header.capture_gen for f in second} == {2}


def test_stop_sends_eos_then_applied_and_exits_cleanly(recs, ident):
    with WorkerProc(recs["two_tone_ci16"], ident, frame_samples=1000, paced=True, loop=True) as w:
        w.read_frame()
        seq = w.send(wc.MsgType.STOP)
        assert wc.unpack_result(wc.MsgType.ACK, w.read_ctl()[1]) is wc.Result.OK
        ap_h, _ = w.read_ctl()
        assert (ap_h.msg_type, ap_h.cmd_seq) == (wc.MsgType.APPLIED, seq)
        while (f := w.read_frame()).header.kind is not FrameKind.END_OF_STREAM:
            pass
        assert w.wait() == 0


def test_sigterm_ends_stream_and_reports_stopping(recs, ident):
    with WorkerProc(recs["two_tone_ci16"], ident, paced=True, loop=True) as w:
        w.read_frame()
        w.proc.send_signal(signal.SIGTERM)
        hdr, p = w.read_ctl()
        assert wc.Status.unpack_payload(p).state is wc.WorkerState.STOPPING
        while w.read_frame().header.kind is not FrameKind.END_OF_STREAM:
            pass
        assert w.wait() == 0


def test_control_socket_close_stops_worker(recs, ident):
    w = WorkerProc(recs["two_tone_ci16"], ident, paced=True, loop=True)
    try:
        w.read_frame()
        w.ctl.close()
        assert w.wait(3) == 0
    finally:
        w.close()


@pytest.mark.parametrize("raw", [
    struct.pack("<IHHHHIQ", 0xDEADBEEF, 1, 24, 3, 0, 0, 1),            # bad magic
    struct.pack("<IHHHHIQ", wc.MAGIC, 1, 24, 0x81, 0, 8, 1),           # worker->control type
    struct.pack("<IHHHHIQ", wc.MAGIC, 1, 24, 3, 0, 4, 1),              # PING with payload
    struct.pack("<IHHHHIQ", wc.MAGIC, 1, 24, 3, 0, 0, 0),              # cmd_seq 0
    struct.pack("<IHHHHIQ", wc.MAGIC, 1, 24, 3, 7, 0, 1),              # reserved0 set
])
def test_malformed_control_message_is_fatal(recs, ident, raw):
    with WorkerProc(recs["two_tone_ci16"], ident, paced=True, loop=True) as w:
        w.ctl.sendall(raw)
        hdr, p = w.read_ctl()
        st = wc.Status.unpack_payload(p)
        assert (st.state, st.result) == (wc.WorkerState.FAILED, wc.Result.E_BAD_MESSAGE)
        assert w.wait() == 65


def test_command_sequence_must_increase(recs, ident):
    with WorkerProc(recs["two_tone_ci16"], ident, paced=True, loop=True) as w:
        w.send(wc.MsgType.PING, seq=5)
        w.read_ctl()
        w.send(wc.MsgType.PING, seq=5)
        st = wc.Status.unpack_payload(w.read_ctl()[1])
        assert st.result is wc.Result.E_BAD_MESSAGE
        assert w.wait() == 65


def test_commands_after_stop_are_refused(recs, ident):
    with WorkerProc(recs["two_tone_ci16"], ident, paced=True, loop=True) as w:
        w.read_frame()
        payload = wc.SetCapture(7_074_000, None).pack_payload()
        # STOP and a SET_CAPTURE back to back in one write.
        w.ctl.sendall(wc.CtlHeader(wc.MsgType.STOP, 0, 1).pack()
                      + wc.CtlHeader(wc.MsgType.SET_CAPTURE, len(payload), 2).pack() + payload)
        msgs = [w.read_ctl() for _ in range(3)]       # ACK(1), APPLIED(1), ACK(2)
        assert [(h.msg_type, h.cmd_seq) for h, _ in msgs] == \
            [(wc.MsgType.ACK, 1), (wc.MsgType.APPLIED, 1), (wc.MsgType.ACK, 2)]
        assert wc.unpack_result(wc.MsgType.ACK, msgs[2][1]) is wc.Result.E_BUSY


# ------------------------------------------------------ startup failures

def test_missing_replay_file_reports_failure(tmp_path, ident, recs):
    rec = recs["two_tone_ci16"]
    bogus = dataclasses.replace(rec, data_path=tmp_path / "missing.sigmf-data")
    with WorkerProc(bogus, ident, expect_running=False) as w:
        st = wc.Status.unpack_payload(w.read_ctl()[1])
        assert (st.state, st.result) == (wc.WorkerState.FAILED, wc.Result.E_DEVICE)
        assert w.wait() == 66


def test_lock_dir_must_exist(tmp_path, recs):
    ident = WorkerIdentity(device_id="replay-test", lock_dir=tmp_path / "nope", receiver_idx=0)
    with WorkerProc(recs["two_tone_ci16"], ident, expect_running=False) as w:
        st = wc.Status.unpack_payload(w.read_ctl()[1])
        assert st.result is wc.Result.E_DEVICE
        assert w.wait() == 73


@pytest.mark.parametrize("bad", [
    ["--device-id", "../etc"], ["--device-id", "UPPER"], ["--format", "cu8"], ["--rate-hz", "0"],
    ["--rate-hz", "48000.0"], ["--rate-hz", "-1"], ["--origin-hz", "7.074e6"], ["--frame-samples", "0"],
    ["--source", "airspyhf"], ["--discontinuity", "0"], ["--bogus", "1"],
    ["--capture-gen-start", "0"], ["--capture-gen-start", str(2**53)],
])
def test_bad_arguments_exit_with_usage(tmp_path, recs, ident, bad):
    rec = recs["two_tone_ci16"]
    argv = replay_argv(WORKER_BIN, rec, ident, ctl_fd=3, data_fd=4)
    # Override: later flags win for single-valued options except where they are validated first.
    i = argv.index(bad[0]) if bad[0] in argv else None
    if i is not None:
        argv[i + 1] = bad[1]
    else:
        argv += bad
    r = subprocess.run(argv, capture_output=True, timeout=5)
    assert r.returncode == 64, r.stderr


def test_discontinuity_beyond_file_is_refused(recs, ident):
    with WorkerProc(recs["two_tone_ci16"], ident, extra_args=["--discontinuity", "999999"],
                    expect_running=False) as w:
        st = wc.Status.unpack_payload(w.read_ctl()[1])
        assert st.result is wc.Result.E_OUT_OF_RANGE
        assert w.wait() == 64


def test_worker_dies_with_its_parent(recs, ident, tmp_path):
    """PR_SET_PDEATHSIG: if the control service dies, the worker exits and frees the device."""
    rec = recs["two_tone_ci16"]
    ctl_a, ctl_b = socket.socketpair()
    data_a, data_b = socket.socketpair()
    argv = replay_argv(WORKER_BIN, rec, ident, ctl_fd=ctl_b.fileno(), data_fd=data_b.fileno(),
                       paced=True, loop=True)
    launcher = (
        "import subprocess, sys, os, time\n"
        f"p = subprocess.Popen({argv!r}, pass_fds=({ctl_b.fileno()}, {data_b.fileno()}))\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(0.3)\n"
        "os._exit(0)\n"
    )
    out = subprocess.run([sys.executable, "-c", launcher], pass_fds=(ctl_b.fileno(), data_b.fileno()),
                         capture_output=True, text=True, timeout=10)
    ctl_b.close(); data_b.close()
    pid = int(out.stdout.strip())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().split()[2]
        except FileNotFoundError:
            break
        if state == "Z":
            break
        time.sleep(0.05)
    else:
        os.kill(pid, signal.SIGKILL)
        pytest.fail("worker outlived its parent")
    ctl_a.close(); data_a.close()
    with WorkerProc(rec, ident) as c:                 # device lock is free again
        c.read_frame()
