"""Browser stream v1: binary WebSocket messages carrying spectrum rows and a session's own audio.

Sent on the control WebSocket (`/ws`) as binary messages, next to the JSON control messages
(text). Spec: docs/protocol/stream-v1.md. Little-endian, every message is

    fixed header (64 bytes) | id (UTF-8, id_len bytes, zero-padded to a multiple of 4) | payload

    off  size  field
      0   u32  magic 0x4253324B ("K2SB" in memory order)
      4   u8   version (1)
      5   u8   kind: 1 SPECTRUM, 2 AUDIO
      6   u16  header_len = 64 + padded id length (payload starts here)
      8   u32  flags: NEW_GEN 1, DISCONTINUITY 2, SERVER_DROP 4
     12   u32  count: bins (SPECTRUM) or int16 samples (AUDIO)
     16   u64  gen: capture_gen (SPECTRUM) or channel_gen (AUDIO)
     24   u64  sample_index: SPECTRUM end sample (exclusive) in the capture; AUDIO first sample
     32   i64  frequency_hz: SPECTRUM capture centre; AUDIO tuned (dial) frequency. Integer Hz.
     40   u32  sample_rate_hz: SPECTRUM capture rate; AUDIO 12000
     44   kind-specific (16 bytes)
            SPECTRUM: i32 db_floor_mdb, u32 db_step_mdb, u32 blocks, u32 reserved (0)
            AUDIO:    i32 level_cdb, i32 pass_lo_hz, i32 pass_hi_hz, u16 mode, u16 agc
     60   u8   id_len (1..64): SPECTRUM receiver id; AUDIO channel id
     61   3    reserved (0)

SPECTRUM payload: `count` uint8 bins, FFT-shifted (bin k is (k - count/2) * rate / count Hz from
the centre); value q means db_floor + q * db_step (milli-dB fields). AUDIO payload: `count`
int16 mono samples at 12000 S/s, pre-volume.

The server never sends a frame whose generation is stale (older capture_gen, or a channel_gen
the channel no longer has). SERVER_DROP marks the first frame of a stream after the server
discarded frames of that stream because the client was not reading fast enough.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum, IntFlag

MAGIC = 0x4253324B
VERSION = 1
FIXED_LEN = 64
MAX_ID_LEN = 64
AUDIO_RATE_HZ = 12000

_FIXED = struct.Struct("<IBBHIIQQqI16sB3x")
_SPEC = struct.Struct("<iIII")
_AUDIO = struct.Struct("<iiiHH")
assert _FIXED.size == FIXED_LEN and _SPEC.size == _AUDIO.size == 16


class Kind(IntEnum):
    SPECTRUM = 1
    AUDIO = 2


class Flags(IntFlag):
    NEW_GEN = 1
    DISCONTINUITY = 2
    SERVER_DROP = 4


DEFINED_FLAGS = 0x7
FLAGS_OFFSET = 8


class StreamError(ValueError):
    """A stream message violates stream v1."""


@dataclass(frozen=True, slots=True)
class SpectrumInfo:
    db_floor_mdb: int
    db_step_mdb: int
    blocks: int


@dataclass(frozen=True, slots=True)
class AudioInfo:
    level_cdb: int
    pass_lo_hz: int
    pass_hi_hz: int
    mode: int
    agc: int


@dataclass(frozen=True, slots=True)
class Message:
    kind: Kind
    flags: Flags
    gen: int
    sample_index: int
    frequency_hz: int
    sample_rate_hz: int
    id: str
    info: SpectrumInfo | AudioInfo
    payload: bytes

    @property
    def count(self) -> int:
        return len(self.payload) if self.kind is Kind.SPECTRUM else len(self.payload) // 2


def _pad4(n: int) -> int:
    return (n + 3) & ~3


def pack(kind: Kind, flags: int, gen: int, sample_index: int, frequency_hz: int, sample_rate_hz: int, ident: str,
         info: SpectrumInfo | AudioInfo, payload: bytes) -> bytes:
    raw_id = ident.encode("utf-8")
    if not 1 <= len(raw_id) <= MAX_ID_LEN:
        raise StreamError(f"id must be 1..{MAX_ID_LEN} bytes")
    if flags & ~DEFINED_FLAGS:
        raise StreamError("undefined flags")
    if kind is Kind.SPECTRUM:
        if not isinstance(info, SpectrumInfo):
            raise StreamError("SPECTRUM needs SpectrumInfo")
        count = len(payload)
        ext = _SPEC.pack(info.db_floor_mdb, info.db_step_mdb, info.blocks, 0)
    else:
        if not isinstance(info, AudioInfo) or len(payload) % 2:
            raise StreamError("AUDIO needs AudioInfo and whole int16 samples")
        if sample_rate_hz != AUDIO_RATE_HZ:
            raise StreamError("AUDIO is 12000 S/s")
        count = len(payload) // 2
        ext = _AUDIO.pack(info.level_cdb, info.pass_lo_hz, info.pass_hi_hz, info.mode, info.agc)
    header_len = FIXED_LEN + _pad4(len(raw_id))
    fixed = _FIXED.pack(MAGIC, VERSION, int(kind), header_len, int(flags), count, gen, sample_index, frequency_hz,
                        sample_rate_hz, ext, len(raw_id))
    return b"".join((fixed, raw_id, b"\0" * (header_len - FIXED_LEN - len(raw_id)), payload))


def with_flags(msg: bytes, extra: int) -> bytes:
    """A copy of a packed message with `extra` flags set (the payload is shared by many sessions)."""
    buf = bytearray(msg)
    (flags,) = struct.unpack_from("<I", buf, FLAGS_OFFSET)
    struct.pack_into("<I", buf, FLAGS_OFFSET, flags | extra)
    return bytes(buf)


def unpack(buf: bytes | bytearray | memoryview) -> Message:
    buf = bytes(buf)
    if len(buf) < FIXED_LEN:
        raise StreamError("short message")
    (magic, version, kind, header_len, flags, count, gen, index, freq, rate, ext, id_len) = _FIXED.unpack_from(buf)
    if magic != MAGIC or version != VERSION:
        raise StreamError("bad magic or version")
    if kind not in (1, 2):
        raise StreamError(f"unknown kind {kind}")
    if flags & ~DEFINED_FLAGS:
        raise StreamError("undefined flags")
    if not 1 <= id_len <= MAX_ID_LEN or header_len != FIXED_LEN + _pad4(id_len) or len(buf) < header_len:
        raise StreamError("bad id length / header_len")
    if any(buf[61:64]) or any(buf[FIXED_LEN + id_len:header_len]):
        raise StreamError("reserved bytes must be zero")
    ident = buf[FIXED_LEN:FIXED_LEN + id_len].decode("utf-8")
    payload = buf[header_len:]
    k = Kind(kind)
    if k is Kind.SPECTRUM:
        floor, step, blocks, reserved = _SPEC.unpack(ext)
        if reserved:
            raise StreamError("reserved bytes must be zero")
        info: SpectrumInfo | AudioInfo = SpectrumInfo(floor, step, blocks)
        if len(payload) != count:
            raise StreamError("payload length does not match count")
    else:
        info = AudioInfo(*_AUDIO.unpack(ext))
        if len(payload) != 2 * count or rate != AUDIO_RATE_HZ:
            raise StreamError("bad AUDIO payload length or rate")
    return Message(k, Flags(flags), gen, index, freq, rate, ident, info, payload)
