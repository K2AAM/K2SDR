"""Malformed data-plane frames and worker control messages are rejected, never misread."""

from __future__ import annotations

import struct

import pytest

from k2websdr.protocol import frame, worker_ctl as wc
from k2websdr.protocol.frame import FrameError, FrameFlags, FrameHeader, FrameKind, SampleFormat


def hdr(**kw) -> FrameHeader:
    base = dict(kind=FrameKind.SAMPLES, format=SampleFormat.CI16, flags=FrameFlags(0), capture_gen=1, seq=0,
                first_sample_index=0, freq_origin_hz=7_074_000, host_time_ns=0, gap_samples=0,
                receiver_idx=0, stream_id=0, sample_rate_hz=768_000, sample_count=1024)
    base.update(kw)
    return FrameHeader(**base)


def patch(buf: bytes, field: str, value: int) -> bytes:
    off = frame.field_offsets()[field]
    code = dict(frame.FIELDS)[field]
    b = bytearray(buf)
    struct.pack_into("<" + code, b, off, value)
    return bytes(b)


def test_round_trip_every_format():
    for fmt in SampleFormat:
        if fmt is SampleFormat.U8DB:                 # spectrum bins only (M1); covered below
            continue
        h = hdr(format=fmt)
        assert FrameHeader.unpack(h.pack()) == h
        assert h.payload_len == 1024 * fmt.bytes_per_sample


def test_u8db_round_trips_in_a_spectrum_frame_only():
    from k2websdr.protocol.frame import SPECTRUM_HEADER_LEN, FrameKind, SpectrumExt
    h = hdr(kind=FrameKind.SPECTRUM, format=SampleFormat.U8DB, header_len=SPECTRUM_HEADER_LEN,
            ext=SpectrumExt(-127500, 500, 3).pack())
    assert FrameHeader.unpack(h.pack()) == h and h.payload_len == 1024
    with pytest.raises(FrameError):
        hdr(format=SampleFormat.U8DB).pack()


@pytest.mark.parametrize("field,value", [
    ("magic", 0x58585858), ("version", 2), ("header_len", 79), ("kind", 9), ("format", 0), ("format", 5),
    ("flags", 1 << 4), ("sample_rate_hz", 0),
])
def test_bad_header_fields(field, value):
    with pytest.raises(FrameError):
        FrameHeader.unpack(patch(hdr().pack(), field, value))


def test_short_buffer():
    with pytest.raises(FrameError):
        FrameHeader.unpack(hdr().pack()[:79])


def test_longer_header_len_is_accepted_for_forward_extension():
    h = FrameHeader.unpack(patch(hdr().pack(), "header_len", 96))
    assert h.header_len == 96


def test_gap_rules():
    with pytest.raises(FrameError):
        hdr(flags=FrameFlags.GAP_KNOWN, gap_samples=10).pack()
    with pytest.raises(FrameError):
        hdr(flags=FrameFlags.DISCONTINUITY, gap_samples=10).pack()
    ok = hdr(flags=FrameFlags.DISCONTINUITY | FrameFlags.GAP_KNOWN, gap_samples=10)
    assert FrameHeader.unpack(ok.pack()).gap_samples == 10


def test_end_of_stream_carries_no_samples():
    with pytest.raises(FrameError):
        hdr(kind=FrameKind.END_OF_STREAM, sample_count=1).pack()
    FrameHeader.unpack(hdr(kind=FrameKind.END_OF_STREAM, sample_count=0).pack())


def test_determinism_key_ignores_only_host_time():
    a, b = hdr(host_time_ns=1), hdr(host_time_ns=2)
    assert a.determinism_key() == b.determinism_key()
    assert hdr(seq=1).determinism_key() != hdr(seq=2).determinism_key()


# ------------------------------------------------------------- worker ctl

def ctl(msg_type: int, payload_len: int, cmd_seq: int, reserved0: int = 0, magic: int = wc.MAGIC) -> bytes:
    return struct.pack("<IHHHHIQ", magic, wc.VERSION, wc.HEADER_LEN_V1, msg_type, reserved0, payload_len, cmd_seq)


@pytest.mark.parametrize("raw", [
    ctl(wc.MsgType.SET_CAPTURE, 16, 0),            # command without cmd_seq
    ctl(wc.MsgType.STATUS, 16, 5),                 # unsolicited with cmd_seq
    ctl(wc.MsgType.SET_CAPTURE, 15, 1),            # wrong payload length
    ctl(wc.MsgType.PING, 4, 1),
    ctl(0x7777, 0, 1),                             # unknown type
    ctl(wc.MsgType.PING, 0, 1, reserved0=1),
    ctl(wc.MsgType.PING, 0, 1, magic=0),
    ctl(wc.MsgType.PING, 0, 1)[:23],
])
def test_bad_ctl_headers(raw):
    with pytest.raises(wc.CtlError):
        wc.CtlHeader.unpack(raw)


def test_set_capture_payload_rules():
    with pytest.raises(wc.CtlError):
        wc.SetCapture(None, None).pack_payload()
    with pytest.raises(wc.CtlError):
        wc.SetCapture(None, 2**32).pack_payload()
    with pytest.raises(wc.CtlError):
        wc.SetCapture.unpack_payload(struct.pack("<qII", 1, 1, 0))       # no flags
    with pytest.raises(wc.CtlError):
        wc.SetCapture.unpack_payload(struct.pack("<qII", 1, 1, 1 << 2))  # reserved flag
    assert wc.SetCapture.unpack_payload(wc.SetCapture(0, None).pack_payload()) == wc.SetCapture(0, None)
    assert wc.SetCapture.unpack_payload(wc.SetCapture(None, 768_000).pack_payload()) == wc.SetCapture(None, 768_000)


def test_failed_never_carries_ok():
    with pytest.raises(wc.CtlError):
        wc.pack_result(wc.MsgType.FAILED, wc.Result.OK)
    with pytest.raises(wc.CtlError):
        wc.unpack_result(wc.MsgType.FAILED, struct.pack("<II", 0, 0))
    assert wc.unpack_result(wc.MsgType.ACK, wc.pack_result(wc.MsgType.ACK, wc.Result.OK)) is wc.Result.OK


def test_applied_verified_is_none_or_readback_only():
    with pytest.raises(wc.CtlError):
        wc.Applied.unpack_payload(struct.pack("<QqII", 1, 7_074_000, 768_000, 2))
