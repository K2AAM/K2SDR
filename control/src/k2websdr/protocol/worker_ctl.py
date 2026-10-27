"""Python mirror of protocol/worker_ctl_v1.h (control <-> worker messages v1).

The C header is canonical; tests/test_abi.py checks this mirror against it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum, IntFlag

MAGIC = 0x4C54434B  # "KCTL" as little-endian u32
VERSION = 1
HEADER_LEN_V1 = 24
MAX_PAYLOAD = 256

HEADER_FIELDS: tuple[tuple[str, str], ...] = (
    ("magic", "I"),
    ("version", "H"),
    ("header_len", "H"),
    ("msg_type", "H"),
    ("reserved0", "H"),
    ("payload_len", "I"),
    ("cmd_seq", "Q"),
)
SET_CAPTURE_FIELDS = (("center_hz", "q"), ("sample_rate_hz", "I"), ("flags", "I"))
ACK_FIELDS = (("result", "I"), ("reserved", "I"))
APPLIED_FIELDS = (("capture_gen", "Q"), ("center_hz", "q"), ("sample_rate_hz", "I"), ("verified", "I"))
FAILED_FIELDS = (("result", "I"), ("reserved", "I"))
STATUS_FIELDS = (("state", "I"), ("result", "I"), ("capture_gen", "Q"))
CHANNEL_FIELDS = (("channel_id", "I"), ("mode", "H"), ("agc", "H"), ("offset_hz", "q"), ("pass_lo_hz", "i"),
                  ("pass_hi_hz", "i"), ("bfo_hz", "i"), ("manual_gain_cdb", "i"), ("flags", "I"),
                  ("reserved", "I"))
CHANNEL_CLOSE_FIELDS = (("channel_id", "I"), ("reserved", "I"))
CHANNEL_APPLIED_FIELDS = (("channel_gen", "Q"), ("capture_gen", "Q")) + CHANNEL_FIELDS + \
    (("channel_rate_hz", "I"), ("open", "I"))
SET_HW_FIELDS = (("flags", "I"), ("hf_agc", "B"), ("hf_agc_threshold", "B"), ("hf_lna", "B"), ("hf_att", "B"),
                 ("reserved", "Q"))
HW_APPLIED_FIELDS = (("capture_gen", "Q"), ("hf_agc", "B"), ("hf_agc_threshold", "B"), ("hf_lna", "B"),
                     ("hf_att", "B"), ("verified", "I"))


def _st(fields: tuple[tuple[str, str], ...]) -> struct.Struct:
    return struct.Struct("<" + "".join(c for _, c in fields))


def offsets(fields: tuple[tuple[str, str], ...]) -> dict[str, int]:
    out, fmt = {}, "<"
    for name, code in fields:
        out[name] = struct.calcsize(fmt)
        fmt += code
    return out


_HDR = _st(HEADER_FIELDS)
_SET_CAPTURE = _st(SET_CAPTURE_FIELDS)
_ACK = _st(ACK_FIELDS)
_APPLIED = _st(APPLIED_FIELDS)
_FAILED = _st(FAILED_FIELDS)
_STATUS = _st(STATUS_FIELDS)
_CHANNEL = _st(CHANNEL_FIELDS)
_CHANNEL_CLOSE = _st(CHANNEL_CLOSE_FIELDS)
_CHANNEL_APPLIED = _st(CHANNEL_APPLIED_FIELDS)
_SET_HW = _st(SET_HW_FIELDS)
_HW_APPLIED = _st(HW_APPLIED_FIELDS)
SIZES = {
    "set_hw": _SET_HW.size, "hw_applied": _HW_APPLIED.size,
    "header": _HDR.size, "set_capture": _SET_CAPTURE.size, "ack": _ACK.size,
    "applied": _APPLIED.size, "failed": _FAILED.size, "status": _STATUS.size,
    "channel": _CHANNEL.size, "channel_close": _CHANNEL_CLOSE.size, "channel_applied": _CHANNEL_APPLIED.size,
}
MAX_CHANNELS = 16
assert SIZES["header"] == HEADER_LEN_V1


class MsgType(IntEnum):
    SET_CAPTURE = 0x0001
    STOP = 0x0002
    PING = 0x0003
    SET_HW = 0x0004
    CHANNEL_OPEN = 0x0010
    CHANNEL_TUNE = 0x0011
    CHANNEL_CLOSE = 0x0012
    ACK = 0x0081
    APPLIED = 0x0082
    FAILED = 0x0083
    PONG = 0x0084
    CHANNEL_APPLIED = 0x0085
    HW_APPLIED = 0x0086
    STATUS = 0x00C1


class Result(IntEnum):
    OK = 0
    E_UNSUPPORTED = 1
    E_OUT_OF_RANGE = 2
    E_DEVICE = 3
    E_BUSY = 4
    E_BAD_MESSAGE = 5
    E_TIMEOUT = 6
    E_NOT_FOUND = 7


class WorkerState(IntEnum):
    STARTING = 1
    RUNNING = 2
    STOPPING = 3
    FAILED = 4


class SetCaptureFlags(IntFlag):
    CENTER = 1 << 0
    RATE = 1 << 1


SC_DEFINED_MASK = 0x3


class HwFlags(IntFlag):
    AGC = 1 << 0
    AGC_THRESHOLD = 1 << 1
    LNA = 1 << 2
    ATT = 1 << 3


HW_DEFINED_MASK = 0xF
HW_ATT_MAX = 8


class Mode(IntEnum):
    AM = 1
    USB = 2
    LSB = 3
    CW = 4
    NFM = 5
    WFM = 6


class Agc(IntEnum):
    OFF = 1
    FAST = 2
    MEDIUM = 3
    SLOW = 4


class ChannelFlags(IntFlag):
    DEFAULT_PASSBAND = 1 << 0
    DEFAULT_BFO = 1 << 1


CH_DEFINED_MASK = 0x3

# Expected payload length per message type (exact; v1 has no variable payloads).
PAYLOAD_LEN = {
    MsgType.SET_CAPTURE: _SET_CAPTURE.size,
    MsgType.STOP: 0,
    MsgType.PING: 0,
    MsgType.CHANNEL_OPEN: _CHANNEL.size,
    MsgType.CHANNEL_TUNE: _CHANNEL.size,
    MsgType.CHANNEL_CLOSE: _CHANNEL_CLOSE.size,
    MsgType.CHANNEL_APPLIED: _CHANNEL_APPLIED.size,
    MsgType.SET_HW: _SET_HW.size,
    MsgType.HW_APPLIED: _HW_APPLIED.size,
    MsgType.ACK: _ACK.size,
    MsgType.APPLIED: _APPLIED.size,
    MsgType.FAILED: _FAILED.size,
    MsgType.PONG: 0,
    MsgType.STATUS: _STATUS.size,
}


class CtlError(ValueError):
    """A worker control message violates worker_ctl v1."""


@dataclass(frozen=True, slots=True)
class CtlHeader:
    msg_type: MsgType
    payload_len: int
    cmd_seq: int

    def pack(self) -> bytes:
        return _HDR.pack(MAGIC, VERSION, HEADER_LEN_V1, int(self.msg_type), 0, self.payload_len, self.cmd_seq)

    @classmethod
    def unpack(cls, buf: bytes | bytearray | memoryview) -> "CtlHeader":
        if len(buf) < HEADER_LEN_V1:
            raise CtlError(f"need {HEADER_LEN_V1} bytes, have {len(buf)}")
        magic, version, header_len, msg_type, reserved0, payload_len, cmd_seq = _HDR.unpack_from(buf, 0)
        if magic != MAGIC:
            raise CtlError(f"bad magic {magic:#010x}")
        if version != VERSION or header_len != HEADER_LEN_V1:
            raise CtlError(f"unsupported ctl version {version}/header_len {header_len}")
        if reserved0 != 0:
            raise CtlError("reserved0 must be 0")
        try:
            mt = MsgType(msg_type)
        except ValueError:
            raise CtlError(f"unknown msg_type {msg_type:#06x}") from None
        if payload_len > MAX_PAYLOAD or payload_len != PAYLOAD_LEN[mt]:
            raise CtlError(f"{mt.name}: payload_len {payload_len} != {PAYLOAD_LEN[mt]}")
        unsolicited = mt is MsgType.STATUS
        if unsolicited and cmd_seq != 0:
            raise CtlError("STATUS must use cmd_seq 0")
        if not unsolicited and cmd_seq == 0:
            raise CtlError(f"{mt.name} requires cmd_seq != 0")
        return cls(mt, payload_len, cmd_seq)


@dataclass(frozen=True, slots=True)
class SetCapture:
    center_hz: int | None
    sample_rate_hz: int | None

    def pack_payload(self) -> bytes:
        flags = SetCaptureFlags(0)
        if self.center_hz is not None:
            flags |= SetCaptureFlags.CENTER
        if self.sample_rate_hz is not None:
            if not 0 < self.sample_rate_hz <= 0xFFFFFFFF:
                raise CtlError("sample_rate_hz out of u32 range")
            flags |= SetCaptureFlags.RATE
        if not flags:
            raise CtlError("SET_CAPTURE must change at least one field")
        return _SET_CAPTURE.pack(self.center_hz or 0, self.sample_rate_hz or 0, int(flags))

    @classmethod
    def unpack_payload(cls, buf: bytes) -> "SetCapture":
        center, rate, flags = _SET_CAPTURE.unpack(buf)
        if flags & ~SC_DEFINED_MASK or not flags:
            raise CtlError(f"bad SET_CAPTURE flags {flags:#x}")
        return cls(center if flags & SetCaptureFlags.CENTER else None,
                   rate if flags & SetCaptureFlags.RATE else None)


@dataclass(frozen=True, slots=True)
class Applied:
    capture_gen: int
    center_hz: int
    sample_rate_hz: int
    verified: int  # 0 none, 1 driver_readback

    def pack_payload(self) -> bytes:
        return _APPLIED.pack(self.capture_gen, self.center_hz, self.sample_rate_hz, self.verified)

    @classmethod
    def unpack_payload(cls, buf: bytes) -> "Applied":
        gen, center, rate, verified = _APPLIED.unpack(buf)
        if verified not in (0, 1):
            raise CtlError(f"bad verified value {verified}")
        return cls(gen, center, rate, verified)


@dataclass(frozen=True, slots=True)
class SetHw:
    """SET_HW payload: Airspy HF+ gain controls. None = leave unchanged (at least one given)."""
    hf_agc: bool | None = None
    hf_agc_threshold_high: bool | None = None
    hf_lna: bool | None = None
    hf_att: int | None = None          # 0..8

    def pack_payload(self) -> bytes:
        flags = HwFlags(0)
        for flag, value in ((HwFlags.AGC, self.hf_agc), (HwFlags.AGC_THRESHOLD, self.hf_agc_threshold_high),
                            (HwFlags.LNA, self.hf_lna), (HwFlags.ATT, self.hf_att)):
            if value is not None:
                flags |= flag
        if not flags:
            raise CtlError("SET_HW must change at least one control")
        if self.hf_att is not None and not 0 <= self.hf_att <= HW_ATT_MAX:
            raise CtlError("hf_att must be 0..8")
        return _SET_HW.pack(int(flags), int(bool(self.hf_agc)), int(bool(self.hf_agc_threshold_high)),
                            int(bool(self.hf_lna)), self.hf_att or 0, 0)


@dataclass(frozen=True, slots=True)
class HwApplied:
    capture_gen: int
    hf_agc: bool
    hf_agc_threshold_high: bool
    hf_lna: bool
    hf_att: int
    verified: int                      # always 0: libairspyhf has no getters

    def pack_payload(self) -> bytes:
        return _HW_APPLIED.pack(self.capture_gen, int(self.hf_agc), int(self.hf_agc_threshold_high),
                                int(self.hf_lna), self.hf_att, self.verified)

    @classmethod
    def unpack_payload(cls, buf: bytes) -> "HwApplied":
        gen, agc, thr, lna, att, verified = _HW_APPLIED.unpack(buf)
        if agc > 1 or thr > 1 or lna > 1 or att > HW_ATT_MAX or verified not in (0, 1):
            raise CtlError("bad HW_APPLIED values")
        return cls(gen, bool(agc), bool(thr), bool(lna), att, verified)


def pack_result(msg: MsgType, result: Result) -> bytes:
    """Payload for ACK or FAILED."""
    if msg is MsgType.FAILED and result is Result.OK:
        raise CtlError("FAILED cannot carry OK")
    return (_ACK if msg is MsgType.ACK else _FAILED).pack(int(result), 0)


def unpack_result(msg: MsgType, buf: bytes) -> Result:
    result, reserved = (_ACK if msg is MsgType.ACK else _FAILED).unpack(buf)
    if reserved != 0:
        raise CtlError("reserved must be 0")
    try:
        r = Result(result)
    except ValueError:
        raise CtlError(f"unknown result {result}") from None
    if msg is MsgType.FAILED and r is Result.OK:
        raise CtlError("FAILED cannot carry OK")
    return r


@dataclass(frozen=True, slots=True)
class Status:
    state: WorkerState
    result: Result
    capture_gen: int

    def pack_payload(self) -> bytes:
        return _STATUS.pack(int(self.state), int(self.result), self.capture_gen)

    @classmethod
    def unpack_payload(cls, buf: bytes) -> "Status":
        state, result, gen = _STATUS.unpack(buf)
        try:
            return cls(WorkerState(state), Result(result), gen)
        except ValueError as e:
            raise CtlError(str(e)) from None


_I32 = (-(1 << 31), (1 << 31) - 1)


@dataclass(frozen=True, slots=True)
class Channel:
    """CHANNEL_OPEN / CHANNEL_TUNE payload. Integer Hz. None = use the worker's default."""
    channel_id: int
    mode: Mode
    agc: Agc
    offset_hz: int
    pass_lo_hz: int | None = None
    pass_hi_hz: int | None = None
    bfo_hz: int | None = None
    manual_gain_cdb: int = 0

    def pack_payload(self) -> bytes:
        if (self.pass_lo_hz is None) != (self.pass_hi_hz is None):
            raise CtlError("give both passband edges or neither")
        flags = ChannelFlags(0)
        if self.pass_lo_hz is None:
            flags |= ChannelFlags.DEFAULT_PASSBAND
        if self.bfo_hz is None:
            flags |= ChannelFlags.DEFAULT_BFO
        for name in ("pass_lo_hz", "pass_hi_hz", "bfo_hz", "manual_gain_cdb"):
            v = getattr(self, name)
            if v is not None and not _I32[0] <= v <= _I32[1]:
                raise CtlError(f"{name} out of i32 range")
        if not 0 <= self.channel_id <= 0xFFFFFFFF:
            raise CtlError("channel_id out of u32 range")
        return _CHANNEL.pack(self.channel_id, int(self.mode), int(self.agc), self.offset_hz,
                             self.pass_lo_hz or 0, self.pass_hi_hz or 0, self.bfo_hz or 0,
                             self.manual_gain_cdb, int(flags), 0)

    @classmethod
    def unpack_payload(cls, buf: bytes) -> "Channel":
        cid, mode, agc, off, lo, hi, bfo, gain, flags, reserved = _CHANNEL.unpack(buf)
        if flags & ~CH_DEFINED_MASK or reserved:
            raise CtlError(f"bad channel flags {flags:#x}/reserved {reserved}")
        try:
            m, a = Mode(mode), Agc(agc)
        except ValueError as e:
            raise CtlError(str(e)) from None
        dp, db = flags & ChannelFlags.DEFAULT_PASSBAND, flags & ChannelFlags.DEFAULT_BFO
        return cls(cid, m, a, off, None if dp else lo, None if dp else hi, None if db else bfo, gain)


def pack_channel_close(channel_id: int) -> bytes:
    return _CHANNEL_CLOSE.pack(channel_id, 0)


@dataclass(frozen=True, slots=True)
class ChannelApplied:
    """What the worker now runs: defaults resolved; open False after CHANNEL_CLOSE."""
    channel_gen: int
    capture_gen: int
    channel: Channel
    channel_rate_hz: int
    open: bool

    def pack_payload(self) -> bytes:
        c = self.channel
        return _CHANNEL_APPLIED.pack(self.channel_gen, self.capture_gen, c.channel_id, int(c.mode), int(c.agc),
                                     c.offset_hz, c.pass_lo_hz or 0, c.pass_hi_hz or 0, c.bfo_hz or 0,
                                     c.manual_gain_cdb, 0, 0, self.channel_rate_hz, int(self.open))

    @classmethod
    def unpack_payload(cls, buf: bytes) -> "ChannelApplied":
        (gen, cgen, cid, mode, agc, off, lo, hi, bfo, gain, flags, reserved, rate, is_open) = \
            _CHANNEL_APPLIED.unpack(buf)
        if flags or reserved or is_open not in (0, 1) or gen < 1:
            raise CtlError("bad CHANNEL_APPLIED")
        try:
            ch = Channel(cid, Mode(mode), Agc(agc), off, lo, hi, bfo, gain)
        except ValueError as e:
            raise CtlError(str(e)) from None
        return cls(gen, cgen, ch, rate, bool(is_open))
