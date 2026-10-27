"""ksdr-worker with M1 DSP: SPECTRUM and AUDIO frames, CHANNEL_OPEN/TUNE/CLOSE (worker_ctl v1).

Runs the real worker over replayed recordings (synthetic and the real Airspy HF+ clips) through
the Python mirrors. K2_WORKER_BIN selects another build (CI also runs this under ASan/UBSan).
"""

from __future__ import annotations

import numpy as np
import pytest

from k2websdr.protocol import worker_ctl as wc
from k2websdr.protocol.frame import FrameFlags, FrameKind, SampleFormat
from k2websdr.replay import sigmf
from k2websdr.replay.worker_argv import WorkerIdentity

from worker_harness import ROOT, Frame, WorkerProc

F = FrameFlags
M, G = wc.Mode, wc.Agc
REAL = ROOT / "fixtures/real/airspyhf"
IQ = ROOT / "fixtures/iq"
A = 12000


@pytest.fixture(scope="module")
def recs() -> dict[str, sigmf.Recording]:
    out = {n: sigmf.load(REAL / f"{n}.sigmf-meta", require_sha512=True)
           for n in ("airspyhf_wwv10_48k", "airspyhf_ft8_40m_48k")}
    out["gap"] = sigmf.load(IQ / "gap_ci16.sigmf-meta", require_sha512=True)
    out["two_tone"] = sigmf.load(IQ / "two_tone_ci16.sigmf-meta", require_sha512=True)
    return out


@pytest.fixture
def ident(tmp_path) -> WorkerIdentity:
    d = tmp_path / "locks"
    d.mkdir()
    return WorkerIdentity(device_id="dsp-test", lock_dir=d, receiver_idx=4, stream_id=11)


class Held(WorkerProc):
    """Worker started with --start-after-ping: configure channels, then `go()`."""

    def go(self) -> None:
        seq = self.send(wc.MsgType.PING)
        hdr, _ = self.read_ctl()
        assert hdr.msg_type is wc.MsgType.PONG and hdr.cmd_seq == seq

    def read_until_eos(self, timeout: float = 10.0):
        if not getattr(self, "_went", False):
            self._went = True
            self.go()
        return super().read_until_eos(timeout)

    def read_frame(self, timeout: float = 5.0):
        if not getattr(self, "_went", False):
            self._went = True
            self.go()
        return super().read_frame(timeout)


def worker(rec, ident, *extra, iq="off", fft=None, fps=10, frame_samples=4800):
    args = ["--iq-frames", iq, "--start-after-ping"] + \
        (["--spectrum-fft", str(fft), "--spectrum-fps", str(fps)] if fft else []) + list(extra)
    return Held(rec, ident, frame_samples=frame_samples, extra_args=args)


def kinds(frames: list[Frame], kind: FrameKind, stream_id: int | None = None) -> list[Frame]:
    return [f for f in frames if f.header.kind is kind and (stream_id is None or f.header.stream_id == stream_id)]


def audio(frames: list[Frame]) -> np.ndarray:
    return np.concatenate([np.frombuffer(f.payload, "<i2") for f in frames]).astype(float) / 32768


def psd(a):
    p = np.abs(np.fft.rfft(a * np.hanning(len(a)))) ** 2
    return np.fft.rfftfreq(len(a), 1 / A), p


def open_ch(w, cid, mode, offset, agc=G.MEDIUM, **kw):
    res, ap = w.channel(wc.MsgType.CHANNEL_OPEN, wc.Channel(cid, mode, agc, offset, **kw))
    assert res is wc.Result.OK, res
    return ap


# ----------------------------------------------------------------------------- spectrum

def test_spectrum_frames_on_real_wwv(recs, ident):
    rec = recs["airspyhf_wwv10_48k"]
    with worker(rec, ident, fft=4096, fps=10) as w:
        frames = w.read_until_eos()
    spec = kinds(frames, FrameKind.SPECTRUM)
    assert not kinds(frames, FrameKind.SAMPLES)                                # --iq-frames off
    assert len(spec) == 50                                                     # 5 s at 10 fps
    h0 = spec[0].header
    assert F.NEW_GEN in h0.flags and h0.format is SampleFormat.U8DB and h0.sample_count == 4096
    assert (h0.freq_origin_hz, h0.sample_rate_hz, h0.stream_id) == (10_001_000, 48000, 0)
    assert [f.header.first_sample_index for f in spec] == [k * 4800 for k in range(1, 51)]
    ext = h0.spectrum_ext
    assert (ext.db_floor_mdb, ext.db_step_mdb, ext.blocks) == (-127500, 500, 1)
    bins = np.mean([np.frombuffer(f.payload, np.uint8).astype(float) for f in spec], axis=0)
    k = int(np.argmax(bins))
    hz = h0.freq_origin_hz + (k - 2048) * h0.sample_rate_hz / 4096
    assert abs(hz - 10_000_000) <= 12                                          # WWV carrier, one bin
    assert [f.header.seq for f in frames] == list(range(len(frames)))          # one counter, all kinds


def test_iq_frames_still_default_on_and_unchanged_when_dsp_runs(recs, ident):
    rec = recs["two_tone"]
    with WorkerProc(rec, ident, frame_samples=4096) as w:
        plain = w.read_until_eos()
    with WorkerProc(rec, ident, frame_samples=4096, extra_args=["--spectrum-fft", "1024"]) as w:
        mixed = w.read_until_eos()
    samples = kinds(mixed, FrameKind.SAMPLES)
    assert [f.payload for f in samples] == [f.payload for f in kinds(plain, FrameKind.SAMPLES)]
    assert [(f.header.first_sample_index, int(f.header.flags)) for f in samples] == \
        [(f.header.first_sample_index, int(f.header.flags)) for f in kinds(plain, FrameKind.SAMPLES)]
    assert kinds(mixed, FrameKind.SPECTRUM)


# ------------------------------------------------------------------------------ channels

def test_am_channel_on_real_wwv_streams_12k_audio_with_metadata(recs, ident):
    rec = recs["airspyhf_wwv10_48k"]
    with worker(rec, ident) as w:
        ap = open_ch(w, 5, M.AM, -1000)
        frames = w.read_until_eos()
    assert ap.open and ap.channel_gen >= 1 and ap.capture_gen == 1 and ap.channel_rate_hz == 12000
    assert (ap.channel.pass_lo_hz, ap.channel.pass_hi_hz, ap.channel.bfo_hz) == (-4500, 4500, 700)
    au = kinds(frames, FrameKind.AUDIO, 5)
    h = au[0].header
    assert F.NEW_GEN in h.flags and h.first_sample_index == 0
    assert (h.sample_rate_hz, h.freq_origin_hz, h.format) == (12000, 10_000_000, SampleFormat.RI16)
    ext = h.audio_ext
    assert (ext.channel_gen, ext.mode, ext.agc, ext.pass_lo_hz, ext.pass_hi_hz) == \
        (ap.channel_gen, M.AM, G.MEDIUM, -4500, 4500)
    idx = [f.header.first_sample_index for f in au]
    lens = [f.header.sample_count for f in au]
    assert idx == list(np.cumsum([0] + lens[:-1]))                             # contiguous
    assert all(F.NEW_GEN not in f.header.flags for f in au[1:])
    a = audio(au)
    assert abs(len(a) - 5 * A) <= 64
    f, p = psd(a[A // 2:])
    med = np.median(p[(f > 100) & (f < 4000)])
    for hz in (600, 100):                                                      # WWV tone and time code
        assert 10 * np.log10(p[np.abs(f - hz) < 2].max() / med) > 20, hz
    assert -80 < au[-1].header.audio_ext.level_dbfs < 0                        # S-meter populated


def test_two_channels_are_independent_and_ordered_by_id(recs, ident):
    rec = recs["airspyhf_ft8_40m_48k"]                                         # centre 7.0755 MHz
    with worker(rec, ident) as w:
        open_ch(w, 9, M.USB, -1500, pass_lo_hz=200, pass_hi_hz=3000)          # dial 7.074 MHz
        open_ch(w, 2, M.CW, -1000)
        frames = w.read_until_eos()
    au = kinds(frames, FrameKind.AUDIO)
    order = [f.header.stream_id for f in au]
    assert order[:2] == [2, 9] and set(order) == {2, 9}
    usb = audio(kinds(frames, FrameKind.AUDIO, 9))[A:13 * A]
    f, p = psd(usb)
    band = (f > 400) & (f < 2900)
    assert 10 * np.log10(p[(f > 3300) & (f < 5500)].mean() / np.median(p[band])) < -55
    assert kinds(frames, FrameKind.AUDIO, 9)[0].header.freq_origin_hz == 7_074_000


def test_offset_only_tune_keeps_generation_other_changes_bump_it(recs, ident):
    # 15 s recording with IQ frames on: the socket's backpressure keeps the worker near the reader,
    # so commands sent mid-stream take effect mid-stream.
    rec = recs["airspyhf_ft8_40m_48k"]
    with worker(rec, ident, iq="on") as w:
        a1 = open_ch(w, 1, M.USB, 1000)
        first = [w.read_frame() for _ in range(3)]
        res, a2 = w.channel(wc.MsgType.CHANNEL_TUNE, wc.Channel(1, M.USB, G.MEDIUM, 1500))
        assert res is wc.Result.OK and a2.channel_gen == a1.channel_gen and a2.channel.offset_hz == 1500
        mid = [w.read_frame() for _ in range(3)]
        res, a3 = w.channel(wc.MsgType.CHANNEL_TUNE, wc.Channel(1, M.LSB, G.MEDIUM, 1500))
        assert res is wc.Result.OK and a3.channel_gen > a2.channel_gen
        rest = w.read_until_eos()
    au = kinds(first + mid + rest, FrameKind.AUDIO, 1)
    gens = [f.header.audio_ext.channel_gen for f in au]
    assert set(gens) == {a1.channel_gen, a3.channel_gen}
    switch = gens.index(a3.channel_gen)
    new = au[switch].header
    assert F.NEW_GEN in new.flags and new.first_sample_index == 0 and new.audio_ext.mode == M.LSB
    before = [f.header.first_sample_index for f in au[:switch]]
    assert before == sorted(before) and len(set(before)) == len(before)       # one contiguous run
    retuned = [f for f in au[:switch] if f.header.freq_origin_hz == rec.freq_origin_hz + 1500]
    assert retuned and all(F.NEW_GEN not in f.header.flags for f in retuned)


def test_close_stops_the_channel_and_reports_it(recs, ident):
    rec = recs["two_tone"]
    with worker(rec, ident) as w:
        open_ch(w, 3, M.AM, 0)
        _ = [w.read_frame() for _ in range(2)]
        res, ap = w.channel(wc.MsgType.CHANNEL_CLOSE, 3)
        assert res is wc.Result.OK and ap.open is False
        rest = w.read_until_eos()
    # Frames already queued before CLOSE may still arrive; none after the close was processed.
    assert len(kinds(rest, FrameKind.AUDIO, 3)) <= 2


@pytest.mark.parametrize("msg, ch, result", [
    (wc.MsgType.CHANNEL_TUNE, wc.Channel(42, M.USB, G.FAST, 0), wc.Result.E_NOT_FOUND),
    (wc.MsgType.CHANNEL_OPEN, wc.Channel(1, M.WFM, G.FAST, 0), wc.Result.E_OUT_OF_RANGE),      # 48k < 192k
    (wc.MsgType.CHANNEL_OPEN, wc.Channel(1, M.USB, G.FAST, 30_000), wc.Result.E_OUT_OF_RANGE),  # outside span
    (wc.MsgType.CHANNEL_OPEN, wc.Channel(1, M.USB, G.FAST, 0, 300, 9000), wc.Result.E_OUT_OF_RANGE),
    (wc.MsgType.CHANNEL_OPEN, wc.Channel(1, M.CW, G.FAST, 0, bfo_hz=50), wc.Result.E_OUT_OF_RANGE),
])
def test_invalid_channel_commands_are_refused_at_ack(recs, ident, msg, ch, result):
    with worker(recs["airspyhf_wwv10_48k"], ident) as w:
        res, ap = w.channel(msg, ch)
        assert res is result and ap is None
        frames = w.read_until_eos()
    assert not kinds(frames, FrameKind.AUDIO)


def test_close_unknown_duplicate_open_and_channel_limit(recs, ident):
    with worker(recs["two_tone"], ident) as w:
        assert w.channel(wc.MsgType.CHANNEL_CLOSE, 77)[0] is wc.Result.E_NOT_FOUND
        open_ch(w, 0, M.USB, 0)
        assert w.channel(wc.MsgType.CHANNEL_OPEN, wc.Channel(0, M.AM, G.FAST, 0))[0] is wc.Result.E_BUSY
        for i in range(1, wc.MAX_CHANNELS):
            open_ch(w, i, M.USB, 0)
        assert w.channel(wc.MsgType.CHANNEL_OPEN, wc.Channel(99, M.USB, G.FAST, 0))[0] is wc.Result.E_BUSY


def test_bad_enum_and_reserved_values_are_refused_without_killing_the_worker(recs, ident):
    import struct
    with worker(recs["two_tone"], ident) as w:
        good = wc.Channel(1, M.USB, G.FAST, 0).pack_payload()
        for payload, want in ((good[:4] + struct.pack("<H", 9) + good[6:], wc.Result.E_UNSUPPORTED),   # mode 9
                              (good[:36] + struct.pack("<I", 1), wc.Result.E_BAD_MESSAGE),              # reserved
                              (good[:32] + struct.pack("<I", 0x80) + good[36:], wc.Result.E_BAD_MESSAGE)):
            seq = w.send(wc.MsgType.CHANNEL_OPEN, payload)
            hdr, p = w.read_ctl()
            assert hdr.cmd_seq == seq and wc.unpack_result(wc.MsgType.ACK, p) is want
        open_ch(w, 1, M.USB, 0)                                                # still alive and serving


def test_reacquisition_rebuilds_channels_with_new_generation(recs, ident):
    rec = recs["airspyhf_ft8_40m_48k"]
    with worker(rec, ident, iq="on", fft=1024) as w:
        a1 = open_ch(w, 1, M.USB, 1000)
        _ = [w.read_frame() for _ in range(4)]
        seq = w.set_capture(rec.freq_origin_hz, rec.sample_rate_hz)
        hdr, p = w.read_ctl()
        assert hdr.msg_type is wc.MsgType.ACK and wc.unpack_result(wc.MsgType.ACK, p) is wc.Result.OK
        hdr, p = w.read_ctl()
        assert hdr.cmd_seq == seq and wc.Applied.unpack_payload(p).capture_gen == 2
        rest = w.read_until_eos()
    au2 = [f for f in kinds(rest, FrameKind.AUDIO, 1) if f.header.capture_gen == 2]
    assert au2 and F.NEW_GEN in au2[0].header.flags and au2[0].header.first_sample_index == 0
    assert au2[0].header.audio_ext.channel_gen > a1.channel_gen
    sp2 = [f for f in kinds(rest, FrameKind.SPECTRUM) if f.header.capture_gen == 2]
    assert F.NEW_GEN in sp2[0].header.flags                                   # spectrum restarted too
    assert sp2[0].header.first_sample_index == rec.sample_rate_hz // 10      # first frame of the new gen


def test_recorded_discontinuity_marks_spectrum_and_audio(recs, ident):
    rec = recs["gap"]                                       # two annotated gaps (one known, one unknown)
    discs = [d.sample_index for d in rec.discontinuities]
    with worker(rec, ident, fft=1024, fps=20, frame_samples=1000) as w:
        open_ch(w, 1, M.USB, 0)
        frames = w.read_until_eos()
    sp = kinds(frames, FrameKind.SPECTRUM)
    flagged = [f.header.first_sample_index for f in sp if F.DISCONTINUITY in f.header.flags]
    assert len(flagged) == len(discs) == 2
    for d, end in zip(discs, flagged):                      # the first frame ending after each gap
        assert end > d and not any(d < f.header.first_sample_index < end for f in sp)
    au = kinds(frames, FrameKind.AUDIO, 1)
    assert sum(F.DISCONTINUITY in f.header.flags for f in au) == 2


def test_dsp_output_is_deterministic(recs, ident):
    rec = recs["airspyhf_ft8_40m_48k"]

    def run():
        with worker(rec, ident, fft=2048) as w:
            open_ch(w, 1, M.USB, -1500)
            open_ch(w, 2, M.AM, 0, agc=G.SLOW)
            return [(f.header.determinism_key(), f.payload) for f in w.read_until_eos()]

    assert run() == run()


@pytest.mark.parametrize("bad", [["--iq-frames", "maybe"], ["--spectrum-fft", "32"], ["--spectrum-fps", "0"],
                                 ["--spectrum-fft", "70000"]])
def test_bad_dsp_arguments_exit_with_usage(recs, ident, bad):
    w = WorkerProc(recs["two_tone"], ident, extra_args=bad, expect_running=False)
    try:
        assert w.wait() == 64
    finally:
        w.close()
