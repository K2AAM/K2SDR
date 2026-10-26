"""Control protocol v1: browser <-> control service, JSON over WebSocket.

Rules enforced here (outline §3, §9, §11):
- Every frequency is a JSON integer in a field named *_hz. Floats, strings,
  booleans and negatives are rejected. Unit strings never appear on the wire;
  they are handled only by k2websdr.protocol.freq on explicit input paths.
- Unknown fields are rejected everywhere (extra="forbid").
- Commands carry a ULID id; the server answers with exactly one `ack`, then,
  if accepted, exactly one `result`.
- State is delivered as a full `snapshot` and then `patch`es with base/rev;
  a client seeing base != its rev discards the patch and resubscribes.
- Hardware fields record requested / applied / status / verification
  separately, so an unacknowledged value can never look applied.

Not yet in v1 (deliberately): hardware gain (its representation differs per
device and is defined after M1/M2 hardware measurement), job/scan operations
(added with the lease work in block 4), listener audio controls (per-session,
never global state).
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Generic, Literal, TypeVar, Union

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_serializer,
    model_validator,
)

from .errors import ErrorCode

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 64 * 1024
MAX_HZ = 10_000_000_000          # global sanity bound; device capability ranges are narrower
MAX_U32 = 0xFFFFFFFF
MAX_JSON_SAFE_INT = 2**53 - 1    # browsers parse JSON numbers as IEEE-754 doubles

# --------------------------------------------------------------------------
# Scalar types
# --------------------------------------------------------------------------

Hz = Annotated[int, Field(strict=True, ge=0, le=MAX_HZ)]
SampleRateHz = Annotated[int, Field(strict=True, gt=0, le=MAX_U32)]
Gen = Annotated[int, Field(strict=True, ge=0, le=MAX_JSON_SAFE_INT)]
Rev = Annotated[int, Field(strict=True, ge=0, le=MAX_JSON_SAFE_INT)]

CmdId = Annotated[str, Field(pattern=r"^[0-9A-HJKMNP-TV-Z]{26}$")]          # ULID, Crockford base32
ReceiverId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]
ChannelId = Annotated[str, Field(pattern=r"^ch_[a-z0-9]{1,32}$")]
SessionId = Annotated[str, Field(pattern=r"^s_[A-Za-z0-9]{1,64}$")]
JobId = Annotated[str, Field(pattern=r"^job_[a-z0-9-]{1,48}$")]
ModeId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")]          # ledger id
PortId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,16}$")]              # device-reported port id
ShortText = Annotated[str, Field(max_length=256)]

FieldStatus = Literal["idle", "pending", "applied", "failed", "unsupported"]
Verification = Literal["none", "driver_readback", "rf_evidence"]
TakeoverPolicy = Literal["deny", "preempt_with_notice"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


# --------------------------------------------------------------------------
# Client -> server: commands
# --------------------------------------------------------------------------

class EmptyArgs(Strict):
    pass


class SessionHelloArgs(Strict):
    client: Annotated[str, Field(max_length=64)]


class ReceiverClaimArgs(Strict):
    receiver: ReceiverId
    takeover: TakeoverPolicy = "deny"


class ReceiverReleaseArgs(Strict):
    receiver: ReceiverId


class ReceiverSetCaptureArgs(Strict):
    receiver: ReceiverId
    center_hz: Hz | None = None
    sample_rate_hz: SampleRateHz | None = None
    if_capture_gen: Gen | None = None   # optimistic precondition -> E_STALE_PRECONDITION

    @model_validator(mode="after")
    def _changes_something(self) -> "ReceiverSetCaptureArgs":
        if self.center_hz is None and self.sample_rate_hz is None:
            raise ValueError("set_capture must change center_hz and/or sample_rate_hz")
        return self


class ReceiverSetAntennaArgs(Strict):
    receiver: ReceiverId
    port: PortId
    if_capture_gen: Gen | None = None


class ChannelOpenArgs(Strict):
    receiver: ReceiverId
    frequency_hz: Hz        # absolute dial frequency; must lie within the current capture span
    mode: ModeId


class ChannelTuneArgs(Strict):
    channel: ChannelId
    frequency_hz: Hz | None = None
    mode: ModeId | None = None
    if_channel_gen: Gen | None = None

    @model_validator(mode="after")
    def _changes_something(self) -> "ChannelTuneArgs":
        if self.frequency_hz is None and self.mode is None:
            raise ValueError("channel.tune must change frequency_hz and/or mode")
        return self


class ChannelCloseArgs(Strict):
    channel: ChannelId


class _Cmd(Strict):
    v: Literal[1]
    type: Literal["cmd"]
    id: CmdId


class SessionHello(_Cmd):
    op: Literal["session.hello"]
    args: SessionHelloArgs


class StateSubscribe(_Cmd):
    op: Literal["state.subscribe"]
    args: EmptyArgs


class ReceiverClaim(_Cmd):
    op: Literal["receiver.claim"]
    args: ReceiverClaimArgs


class ReceiverRelease(_Cmd):
    op: Literal["receiver.release"]
    args: ReceiverReleaseArgs


class ReceiverSetCapture(_Cmd):
    op: Literal["receiver.set_capture"]
    args: ReceiverSetCaptureArgs


class ReceiverSetAntenna(_Cmd):
    op: Literal["receiver.set_antenna"]
    args: ReceiverSetAntennaArgs


class ChannelOpen(_Cmd):
    op: Literal["channel.open"]
    args: ChannelOpenArgs


class ChannelTune(_Cmd):
    op: Literal["channel.tune"]
    args: ChannelTuneArgs


class ChannelClose(_Cmd):
    op: Literal["channel.close"]
    args: ChannelCloseArgs


ClientMessage = Annotated[
    Union[
        SessionHello, StateSubscribe, ReceiverClaim, ReceiverRelease,
        ReceiverSetCapture, ReceiverSetAntenna, ChannelOpen, ChannelTune, ChannelClose,
    ],
    Field(discriminator="op"),
]

# Permission each op requires (enforced by the server in block 5; recorded here
# so the protocol and the authorization table cannot drift apart silently).
OP_PERMISSION: dict[str, str | None] = {
    "session.hello": None,
    "state.subscribe": "listen",
    "receiver.claim": "hw.retune",
    "receiver.release": "hw.retune",
    "receiver.set_capture": "hw.retune",
    "receiver.set_antenna": "hw.antenna",
    "channel.open": "listen",
    "channel.tune": "listen",
    "channel.close": "listen",
}

# --------------------------------------------------------------------------
# Server state
# --------------------------------------------------------------------------

T = TypeVar("T")


class FieldState(Strict, Generic[T]):
    """One hardware-controlled value. `applied` is the only value a UI may show as in effect."""

    requested: T | None = None
    applied: T | None = None
    status: FieldStatus = "idle"
    cmd: CmdId | None = None             # command currently or last responsible
    verified: Verification = "none"
    error: ErrorCode | None = None

    @model_validator(mode="after")
    def _invariants(self) -> "FieldState[T]":
        if self.status == "pending" and self.cmd is None:
            raise ValueError("pending field must reference its command")
        if self.status == "failed" and self.error is None:
            raise ValueError("failed field must carry an error code")
        if self.status != "failed" and self.error is not None:
            raise ValueError("error is only set on failed fields")
        if self.status == "unsupported" and (self.requested is not None or self.applied is not None):
            raise ValueError("unsupported field carries no values")
        if self.applied is None and self.verified != "none":
            raise ValueError("cannot verify an unapplied value")
        return self


class AntennaPort(Strict):
    id: PortId
    label: Annotated[str, Field(max_length=64)]   # custom label, e.g. "C — HF"


class Capabilities(Strict):
    freq_range_hz: tuple[Hz, Hz] | None = None
    sample_rates_hz: list[SampleRateHz] = []
    antennas: list[AntennaPort] = []
    sample_format: Literal["ci16", "cf32", "ri16", "rf32"] | None = None


class Acquisition(Strict):
    state: Literal["disabled", "stopped", "starting", "running", "failed", "external_busy"]
    detail: ShortText | None = None


class LeaseHolder(Strict):
    kind: Literal["operator", "job"]
    principal: Annotated[str, Field(max_length=128)]
    session: SessionId | None = None
    job: JobId | None = None

    @model_validator(mode="after")
    def _kind_fields(self) -> "LeaseHolder":
        if self.kind == "operator" and (self.session is None or self.job is not None):
            raise ValueError("operator lease needs session and no job")
        if self.kind == "job" and (self.job is None or self.session is not None):
            raise ValueError("job lease needs job and no session (jobs outlive sessions)")
        return self


class Lease(Strict):
    holder: LeaseHolder | None = None
    since_utc: AwareDatetime | None = None
    takeover_policy: TakeoverPolicy = "deny"


class Capture(Strict):
    center_hz: FieldState[int]
    sample_rate_hz: FieldState[int]
    antenna: FieldState[str]


class ReceiverState(Strict):
    label: Annotated[str, Field(max_length=64)]
    driver: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,32}$")]
    acquisition: Acquisition
    lease: Lease
    capture_gen: Gen
    capture: Capture
    capabilities: Capabilities


class ChannelOwner(Strict):
    session: SessionId | None = None
    job: JobId | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "ChannelOwner":
        if (self.session is None) == (self.job is None):
            raise ValueError("channel owner is exactly one of session or job")
        return self


class ChannelState(Strict):
    receiver: ReceiverId
    owner: ChannelOwner
    frequency_hz: Hz
    mode: ModeId
    channel_gen: Gen
    decoder_taps: list[ModeId] = []   # taps sit at demod output, upstream of all listener audio controls


class ServerInfo(Strict):
    protocol: Literal[1]
    build: Annotated[str, Field(max_length=64)]
    exposure: Literal["loopback", "private_network", "public"]


class LedgerSummary(Strict):
    version: Annotated[int, Field(strict=True, ge=1)]
    available: list[ModeId] = []


class ServerState(Strict):
    server: ServerInfo
    receivers: dict[ReceiverId, ReceiverState] = {}
    channels: dict[ChannelId, ChannelState] = {}
    ledger: LedgerSummary


# --------------------------------------------------------------------------
# Server -> client messages
# --------------------------------------------------------------------------

class _Srv(Strict):
    v: Literal[1]


class Ack(_Srv):
    type: Literal["ack"]
    id: CmdId
    status: Literal["accepted", "rejected"]
    code: ErrorCode | None = None
    detail: ShortText | None = None
    rev: Rev | None = None

    @model_validator(mode="after")
    def _code_iff_rejected(self) -> "Ack":
        if (self.status == "rejected") != (self.code is not None):
            raise ValueError("code is required on rejected acks and forbidden on accepted ones")
        return self


class Result(_Srv):
    type: Literal["result"]
    id: CmdId
    status: Literal["applied", "failed"]
    code: ErrorCode | None = None
    detail: ShortText | None = None
    rev: Rev
    capture_gen: Gen | None = None
    channel: ChannelId | None = None      # set by channel.open
    channel_gen: Gen | None = None

    @model_validator(mode="after")
    def _code_iff_failed(self) -> "Result":
        if (self.status == "failed") != (self.code is not None):
            raise ValueError("code is required on failed results and forbidden on applied ones")
        return self


class ErrorMsg(_Srv):
    """A message the server could not attribute to a command (e.g. unparseable input)."""
    type: Literal["error"]
    code: ErrorCode
    detail: ShortText | None = None
    id: CmdId | None = None


class Snapshot(_Srv):
    type: Literal["snapshot"]
    rev: Rev
    state: ServerState


JsonPointer = Annotated[str, Field(pattern=r"^(/([^/~]|~[01])*)+$", max_length=512)]


class PatchOp(Strict):
    op: Literal["add", "replace", "remove"]
    path: JsonPointer
    value: Any = None

    @model_validator(mode="before")
    @classmethod
    def _value_presence(cls, data: Any) -> Any:
        if isinstance(data, dict):
            has_value = "value" in data
            if data.get("op") == "remove" and has_value:
                raise ValueError("remove must not carry a value")
            if data.get("op") in ("add", "replace") and not has_value:
                raise ValueError(f"{data.get('op')} requires a value")
        return data

    @model_serializer(mode="wrap")
    def _omit_remove_value(self, handler: Any) -> Any:
        out = handler(self)
        if self.op == "remove":
            out.pop("value", None)
        return out


class Patch(_Srv):
    type: Literal["patch"]
    base: Rev
    rev: Rev
    ops: Annotated[list[PatchOp], Field(min_length=1)]

    @model_validator(mode="after")
    def _consecutive(self) -> "Patch":
        if self.rev != self.base + 1:
            raise ValueError("patch rev must equal base + 1")
        return self


ServerMessage = Annotated[
    Union[Ack, Result, ErrorMsg, Snapshot, Patch],
    Field(discriminator="type"),
]

CLIENT_ADAPTER: TypeAdapter[Any] = TypeAdapter(ClientMessage)
SERVER_ADAPTER: TypeAdapter[Any] = TypeAdapter(ServerMessage)

# --------------------------------------------------------------------------
# Wire helpers
# --------------------------------------------------------------------------


class ProtocolError(ValueError):
    def __init__(self, code: ErrorCode, detail: str, cmd_id: str | None = None) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail[:256]
        self.cmd_id = cmd_id


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key {k!r}")
        out[k] = v
    return out


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-JSON number {name}")


def parse_client_message(raw: str | bytes) -> Any:
    """Parse one inbound WebSocket text frame into a typed command.

    Raises ProtocolError with a wire code. Never raises anything else for
    hostile input.
    """
    size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
    if size > MAX_MESSAGE_BYTES:
        raise ProtocolError(ErrorCode.E_PAYLOAD_TOO_LARGE, f"message exceeds {MAX_MESSAGE_BYTES} bytes")
    try:
        obj = json.loads(raw, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
    except (ValueError, RecursionError, UnicodeDecodeError) as e:
        raise ProtocolError(ErrorCode.E_SCHEMA, f"invalid JSON: {e}") from None
    if not isinstance(obj, dict):
        raise ProtocolError(ErrorCode.E_SCHEMA, "message must be a JSON object")
    cmd_id = obj.get("id") if isinstance(obj.get("id"), str) and len(obj["id"]) == 26 else None
    if "v" in obj and obj["v"] != PROTOCOL_VERSION:
        raise ProtocolError(ErrorCode.E_UNSUPPORTED_VERSION,
                            f"protocol v{PROTOCOL_VERSION} only", cmd_id)
    try:
        return CLIENT_ADAPTER.validate_python(obj, strict=True)
    except ValidationError as e:
        first = e.errors(include_url=False, include_context=False, include_input=False)[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise ProtocolError(ErrorCode.E_SCHEMA, f"{loc}: {first['msg']}", cmd_id) from None


def dump_server_message(msg: BaseModel) -> str:
    """Serialize a server message after re-validating it, so the server cannot emit invalid output."""
    text = msg.model_dump_json()
    SERVER_ADAPTER.validate_json(text, strict=True)
    return text
