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
SIZES = {
    "header": _HDR.size, "set_capture": _SET_CAPTURE.size, "ack": _ACK.size,
    "applied": _APPLIED.size, "failed": _FAILED.size, "status": _STATUS.size,
}
assert SIZES["header"] == HEADER_LEN_V1


class MsgType(IntEnum):
    SET_CAPTURE = 0x0001
    STOP = 0x0002
    PING = 0x0003
    ACK = 0x0081
    APPLIED = 0x0082
    FAILED = 0x0083
    PONG = 0x0084
    STATUS = 0x00C1


class Result(IntEnum):
    OK = 0
    E_UNSUPPORTED = 1
    E_OUT_OF_RANGE = 2
    E_DEVICE = 3
    E_BUSY = 4
    E_BAD_MESSAGE = 5
    E_TIMEOUT = 6


class WorkerState(IntEnum):
    STARTING = 1
    RUNNING = 2
    STOPPING = 3
    FAILED = 4


class SetCaptureFlags(IntFlag):
    CENTER = 1 << 0
    RATE = 1 << 1


SC_DEFINED_MASK = 0x3

# Expected payload length per message type (exact; v1 has no variable payloads).
PAYLOAD_LEN = {
    MsgType.SET_CAPTURE: _SET_CAPTURE.size,
    MsgType.STOP: 0,
    MsgType.PING: 0,
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
