"""Browser stream v1 codec (k2websdr.protocol.stream; docs/protocol/stream-v1.md)."""

from __future__ import annotations

import struct

import pytest

from k2websdr.protocol import stream as sv

SPEC = sv.SpectrumInfo(-127500, 500, 14)
AUD = sv.AudioInfo(-4321, 300, 2700, 2, 3)


def test_spectrum_layout_is_exactly_as_documented():
    b = sv.pack(sv.Kind.SPECTRUM, sv.Flags.NEW_GEN, 7, 384000, 7_074_000, 48000, "replay-0", SPEC, bytes(range(16)))
    assert len(b) == 64 + 8 + 16
    assert b[:4] == b"K2SB"
    assert struct.unpack_from("<BBHIIQQqI", b, 4) == (1, 1, 72, 1, 16, 7, 384000, 7_074_000, 48000)
    assert struct.unpack_from("<iIII", b, 44) == (-127500, 500, 14, 0)
    assert b[60] == 8 and b[61:64] == b"\0\0\0" and b[64:72] == b"replay-0"
    assert b[72:] == bytes(range(16))


def test_audio_layout_and_id_padding():
    pcm = struct.pack("<3h", 1, -2, 32767)
    b = sv.pack(sv.Kind.AUDIO, 0, 2**53, 12, 10_000_000, 12000, "ch_1", AUD, pcm)
    assert struct.unpack_from("<H", b, 6)[0] == 68 and len(b) == 68 + 6
    assert struct.unpack_from("<I", b, 12)[0] == 3
    assert struct.unpack_from("<iiiHH", b, 44) == (-4321, 300, 2700, 2, 3)
    b5 = sv.pack(sv.Kind.AUDIO, 0, 1, 0, 1, 12000, "ch_12", AUD, pcm)       # 5-byte id -> padded to 8
    assert struct.unpack_from("<H", b5, 6)[0] == 72 and b5[69:72] == b"\0\0\0"


@pytest.mark.parametrize("kind, info, payload, ident", [
    (sv.Kind.SPECTRUM, SPEC, bytes(2048), "r" * 64),
    (sv.Kind.AUDIO, AUD, bytes(2048), "ch_abc"),
])
def test_round_trip(kind, info, payload, ident):
    rate = 12000 if kind is sv.Kind.AUDIO else 384000
    b = sv.pack(kind, sv.Flags.DISCONTINUITY, 3, 99, -1, rate, ident, info, payload)
    msg = sv.unpack(b)
    assert (msg.kind, msg.flags, msg.gen, msg.sample_index, msg.frequency_hz, msg.id, msg.info, msg.payload) == (
        kind, sv.Flags.DISCONTINUITY, 3, 99, -1, ident, info, payload)
    assert msg.count == (len(payload) if kind is sv.Kind.SPECTRUM else len(payload) // 2)


def test_with_flags_copies_and_sets():
    b = sv.pack(sv.Kind.AUDIO, sv.Flags.NEW_GEN, 1, 0, 1, 12000, "ch_1", AUD, b"\0\0")
    f = sv.with_flags(b, sv.Flags.SERVER_DROP)
    assert sv.unpack(f).flags == sv.Flags.NEW_GEN | sv.Flags.SERVER_DROP
    assert sv.unpack(b).flags == sv.Flags.NEW_GEN                         # the shared original is untouched


def test_encoder_refuses_bad_messages():
    for args in ((sv.Kind.SPECTRUM, 0, 1, 0, 1, 1, "", SPEC, b""),               # empty id
                 (sv.Kind.SPECTRUM, 0, 1, 0, 1, 1, "x" * 65, SPEC, b""),         # id too long
                 (sv.Kind.SPECTRUM, 8, 1, 0, 1, 1, "x", SPEC, b""),              # undefined flag
                 (sv.Kind.AUDIO, 0, 1, 0, 1, 12000, "x", AUD, b"\0"),            # half a sample
                 (sv.Kind.AUDIO, 0, 1, 0, 1, 48000, "x", AUD, b""),              # audio is 12 kS/s
                 (sv.Kind.AUDIO, 0, 1, 0, 1, 12000, "x", SPEC, b"")):            # wrong info
        with pytest.raises(sv.StreamError):
            sv.pack(*args)


def test_decoder_refuses_malformed_messages():
    good = sv.pack(sv.Kind.SPECTRUM, 0, 1, 0, 1, 48000, "rx", SPEC, b"\1\2\3")
    bad = [good[:63], b"XXXX" + good[4:], good[:4] + b"\2" + good[5:], good[:5] + b"\3" + good[6:],
           good[:8] + struct.pack("<I", 8) + good[12:], good[:61] + b"\1" + good[62:],
           good[:66] + b"\1" + good[67:],                                      # id padding not zero
           good[:-1],                                                         # payload shorter than count
           good[:56] + b"\1" + good[57:]]                                     # spectrum reserved
    for b in bad:
        with pytest.raises(sv.StreamError):
            sv.unpack(b)
