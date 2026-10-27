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
    SPECTRUM = 3   # M1: format U8DB, extension SpectrumExt
    AUDIO = 4      # M1: format RI16 mono 12 kS/s, extension AudioExt


class SampleFormat(IntEnum):
    CI16 = 1
    CF32 = 2
    RI16 = 3
    RF32 = 4
    U8DB = 5       # spectrum bin: dBFS = db_floor + q * db_step

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
    SampleFormat.U8DB: 1,
}

# Kind-specific header extensions (immediately after the 80-byte header).
SPECTRUM_EXT_FIELDS = (("db_floor_mdb", "i"), ("db_step_mdb", "I"), ("blocks", "I"), ("reserved", "I"))
AUDIO_EXT_FIELDS = (("channel_gen", "Q"), ("mode", "H"), ("agc", "H"), ("level_cdb", "i"),
                    ("pass_lo_hz", "i"), ("pass_hi_hz", "i"))
_SPECTRUM_EXT = struct.Struct("<" + "".join(c for _, c in SPECTRUM_EXT_FIELDS))
_AUDIO_EXT = struct.Struct("<" + "".join(c for _, c in AUDIO_EXT_FIELDS))
SPECTRUM_HEADER_LEN = HEADER_LEN_V1 + _SPECTRUM_EXT.size   # 96
AUDIO_HEADER_LEN = HEADER_LEN_V1 + _AUDIO_EXT.size         # 104
AUDIO_RATE_HZ = 12000

# kind -> (required format, minimum header_len)
_KIND_RULES = {
    FrameKind.SPECTRUM: (SampleFormat.U8DB, SPECTRUM_HEADER_LEN),
    FrameKind.AUDIO: (SampleFormat.RI16, AUDIO_HEADER_LEN),
}


def ext_offsets(fields: tuple[tuple[str, str], ...]) -> dict[str, int]:
    out: dict[str, int] = {}
    fmt = "<"
    for name, code in fields:
        out[name] = struct.calcsize(fmt)
        fmt += code
    return out


@dataclass(frozen=True, slots=True)
class SpectrumExt:
    db_floor_mdb: int
    db_step_mdb: int
    blocks: int

    @classmethod
    def unpack(cls, buf: bytes) -> "SpectrumExt":
        if len(buf) < _SPECTRUM_EXT.size:
            raise FrameError("spectrum extension truncated")
        floor, step, blocks, reserved = _SPECTRUM_EXT.unpack_from(buf, 0)
        if step <= 0 or blocks < 1 or reserved != 0:
            raise FrameError(f"bad spectrum extension step={step} blocks={blocks} reserved={reserved}")
        return cls(floor, step, blocks)

    def pack(self) -> bytes:
        return _SPECTRUM_EXT.pack(self.db_floor_mdb, self.db_step_mdb, self.blocks, 0)

    def db(self, q: int) -> float:
        """dBFS of quantized bin value q."""
        return (self.db_floor_mdb + q * self.db_step_mdb) / 1000.0


@dataclass(frozen=True, slots=True)
class AudioExt:
    channel_gen: int
    mode: int
    agc: int
    level_cdb: int
    pass_lo_hz: int
    pass_hi_hz: int

    @classmethod
    def unpack(cls, buf: bytes) -> "AudioExt":
        if len(buf) < _AUDIO_EXT.size:
            raise FrameError("audio extension truncated")
        gen, mode, agc, level, lo, hi = _AUDIO_EXT.unpack_from(buf, 0)
        if gen < 1 or lo >= hi:
            raise FrameError(f"bad audio extension channel_gen={gen} passband={lo}..{hi}")
        return cls(gen, mode, agc, level, lo, hi)

    def pack(self) -> bytes:
        return _AUDIO_EXT.pack(self.channel_gen, self.mode, self.agc, self.level_cdb, self.pass_lo_hz,
                               self.pass_hi_hz)

    @property
    def level_dbfs(self) -> float:
        return self.level_cdb / 100.0


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
    ext: bytes = b""    # header bytes after the first 80 (kind extension, then anything newer)

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
        rule = _KIND_RULES.get(self.kind)
        if rule is not None:
            fmt, min_len = rule
            if self.format is not fmt:
                raise FrameError(f"{self.kind.name} frames must use format {fmt.name}")
            if self.header_len < min_len:
                raise FrameError(f"{self.kind.name} header_len {self.header_len} < {min_len}")
        elif self.format is SampleFormat.U8DB:
            raise FrameError("U8DB is only valid for SPECTRUM frames")
        if self.kind is FrameKind.AUDIO and self.sample_rate_hz != AUDIO_RATE_HZ:
            raise FrameError(f"AUDIO sample_rate_hz must be {AUDIO_RATE_HZ}")

    @property
    def spectrum_ext(self) -> SpectrumExt:
        if self.kind is not FrameKind.SPECTRUM:
            raise FrameError("not a SPECTRUM frame")
        return SpectrumExt.unpack(self.ext)

    @property
    def audio_ext(self) -> AudioExt:
        if self.kind is not FrameKind.AUDIO:
            raise FrameError("not an AUDIO frame")
        return AudioExt.unpack(self.ext)

    def pack(self) -> bytes:
        """Pack a v1 header, including `ext` (header_len must be 80 + len(ext))."""
        self.validate()
        if self.header_len != HEADER_LEN_V1 + len(self.ext):
            raise FrameError("header_len must equal 80 + len(ext)")
        return _STRUCT.pack(
            MAGIC, VERSION, self.header_len, int(self.kind), int(self.format), int(self.flags),
            self.capture_gen, self.seq, self.first_sample_index, self.freq_origin_hz,
            self.host_time_ns, self.gap_samples, self.receiver_idx, self.stream_id,
            self.sample_rate_hz, self.sample_count,
        ) + self.ext

    @classmethod
    def unpack(cls, buf: bytes | bytearray | memoryview) -> "FrameHeader":
        """Decode and validate a header from the first bytes of buf.

        The caller reads header_len bytes in total (80 + the kind's extension + anything a newer
        minor adds) and attaches the bytes after the first 80 with `with_ext` before reading
        payload_len payload bytes. Kind-specific checks that need the extension run there.
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
        if len(buf) >= header_len:                     # whole header supplied: attach the extension
            hdr = hdr.with_ext(bytes(buf[HEADER_LEN_V1:header_len]))
        return hdr

    def with_ext(self, ext: bytes) -> "FrameHeader":
        """Attach the header bytes after the first 80 and check the kind's extension."""
        if len(ext) != self.header_len - HEADER_LEN_V1:
            raise FrameError(f"extension is {len(ext)} bytes, header_len says {self.header_len - HEADER_LEN_V1}")
        hdr = FrameHeader(**{**{f: getattr(self, f) for f in self.__slots__ if f != "ext"}, "ext": bytes(ext)})
        if hdr.kind is FrameKind.SPECTRUM:
            hdr.spectrum_ext   # noqa: B018  validates
        elif hdr.kind is FrameKind.AUDIO:
            hdr.audio_ext      # noqa: B018
        return hdr

    def determinism_key(self) -> tuple:
        """Every field except host_time_ns, for byte-identical replay comparisons."""
        return (self.kind, self.format, int(self.flags), self.capture_gen, self.seq,
                self.first_sample_index, self.freq_origin_hz, self.gap_samples,
                self.receiver_idx, self.stream_id, self.sample_rate_hz, self.sample_count,
                self.header_len, self.ext)
