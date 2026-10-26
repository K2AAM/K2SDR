"""Python mirror of protocol/frame_v1.h (data-plane frame header v1).

The C header is canonical. tests/test_abi.py compiles it and checks that the
offsets, sizes and constants here match exactly.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum, IntFlag

MAGIC = 0x5244534B  # "KSDR" as little-endian u32
VERSION = 1
HEADER_LEN_V1 = 80

# (field name, struct code) in wire order. Offsets are derived, not hand-written.
FIELDS: tuple[tuple[str, str], ...] = (
    ("magic", "I"),
    ("version", "H"),
    ("header_len", "H"),
    ("kind", "H"),
    ("format", "H"),
    ("flags", "I"),
    ("capture_gen", "Q"),
    ("seq", "Q"),
    ("first_sample_index", "Q"),
    ("freq_origin_hz", "q"),
    ("host_time_ns", "Q"),
    ("gap_samples", "Q"),
    ("receiver_idx", "I"),
    ("stream_id", "I"),
    ("sample_rate_hz", "I"),
    ("sample_count", "I"),
)
_STRUCT = struct.Struct("<" + "".join(code for _, code in FIELDS))
assert _STRUCT.size == HEADER_LEN_V1


def field_offsets() -> dict[str, int]:
    out: dict[str, int] = {}
    fmt = "<"
    for name, code in FIELDS:
        out[name] = struct.calcsize(fmt)
        fmt += code
    return out


class FrameKind(IntEnum):
    SAMPLES = 1
    END_OF_STREAM = 2


class SampleFormat(IntEnum):
    CI16 = 1
    CF32 = 2
    RI16 = 3
    RF32 = 4

    @property
    def bytes_per_sample(self) -> int:
        return _BYTES_PER_SAMPLE[self]

    @property
    def is_complex(self) -> bool:
        return self in (SampleFormat.CI16, SampleFormat.CF32)


_BYTES_PER_SAMPLE = {
    SampleFormat.CI16: 4,
    SampleFormat.CF32: 8,
    SampleFormat.RI16: 2,
    SampleFormat.RF32: 4,
}


class FrameFlags(IntFlag):
    DISCONTINUITY = 1 << 0
    GAP_KNOWN = 1 << 1
    OVERFLOW = 1 << 2
    NEW_GEN = 1 << 3


FLAGS_DEFINED_MASK = 0x0000000F


class FrameError(ValueError):
    """A frame header violates frame v1."""


@dataclass(frozen=True, slots=True)
class FrameHeader:
    kind: FrameKind
    format: SampleFormat
    flags: FrameFlags
    capture_gen: int
    seq: int
    first_sample_index: int
    freq_origin_hz: int
    host_time_ns: int
    gap_samples: int
    receiver_idx: int
    stream_id: int
    sample_rate_hz: int
    sample_count: int
    header_len: int = HEADER_LEN_V1

    @property
    def payload_len(self) -> int:
        return self.sample_count * self.format.bytes_per_sample

    def validate(self) -> None:
        if self.header_len < HEADER_LEN_V1:
            raise FrameError(f"header_len {self.header_len} < {HEADER_LEN_V1}")
        if int(self.flags) & ~FLAGS_DEFINED_MASK:
            raise FrameError(f"reserved flag bits set: {int(self.flags):#x}")
        if FrameFlags.GAP_KNOWN in self.flags and FrameFlags.DISCONTINUITY not in self.flags:
            raise FrameError("GAP_KNOWN requires DISCONTINUITY")
        if FrameFlags.GAP_KNOWN not in self.flags and self.gap_samples != 0:
            raise FrameError("gap_samples must be 0 unless GAP_KNOWN")
        if self.kind is FrameKind.END_OF_STREAM and self.sample_count != 0:
            raise FrameError("END_OF_STREAM must carry no samples")
        if self.sample_rate_hz <= 0:
            raise FrameError("sample_rate_hz must be > 0")

    def pack(self) -> bytes:
        """Pack a v1 header. Only header_len == 80 is produced by this writer."""
        self.validate()
        if self.header_len != HEADER_LEN_V1:
            raise FrameError("this writer only emits 80-byte v1 headers")
        return _STRUCT.pack(
            MAGIC, VERSION, HEADER_LEN_V1, int(self.kind), int(self.format), int(self.flags),
            self.capture_gen, self.seq, self.first_sample_index, self.freq_origin_hz,
            self.host_time_ns, self.gap_samples, self.receiver_idx, self.stream_id,
            self.sample_rate_hz, self.sample_count,
        )

    @classmethod
    def unpack(cls, buf: bytes | bytearray | memoryview) -> "FrameHeader":
        """Decode and validate a header from the first bytes of buf.

        The caller skips header_len bytes (which may exceed 80 for a future
        compatible minor extension) before reading payload_len sample bytes.
        """
        if len(buf) < HEADER_LEN_V1:
            raise FrameError(f"need {HEADER_LEN_V1} bytes, have {len(buf)}")
        (magic, version, header_len, kind, fmt, flags, capture_gen, seq, first_idx, origin,
         host_ns, gap, rx_idx, stream_id, rate, count) = _STRUCT.unpack_from(buf, 0)
        if magic != MAGIC:
            raise FrameError(f"bad magic {magic:#010x}")
        if version != VERSION:
            raise FrameError(f"unsupported frame version {version}")
        try:
            kind_e = FrameKind(kind)
        except ValueError:
            raise FrameError(f"unknown frame kind {kind}") from None
        try:
            fmt_e = SampleFormat(fmt)
        except ValueError:
            raise FrameError(f"unknown sample format {fmt}") from None
        if flags & ~FLAGS_DEFINED_MASK:
            raise FrameError(f"reserved flag bits set: {flags:#x}")
        hdr = cls(
            kind=kind_e, format=fmt_e, flags=FrameFlags(flags),
            capture_gen=capture_gen, seq=seq, first_sample_index=first_idx, freq_origin_hz=origin,
            host_time_ns=host_ns, gap_samples=gap, receiver_idx=rx_idx, stream_id=stream_id,
            sample_rate_hz=rate, sample_count=count, header_len=header_len,
        )
        hdr.validate()
        return hdr

    def determinism_key(self) -> tuple:
        """Every field except host_time_ns, for byte-identical replay comparisons."""
        return (self.kind, self.format, int(self.flags), self.capture_gen, self.seq,
                self.first_sample_index, self.freq_origin_hz, self.gap_samples,
                self.receiver_idx, self.stream_id, self.sample_rate_hz, self.sample_count)
