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

Not yet in v1 (deliberately): hardware gain and antenna wire messages to the
worker (defined with M1/M2 hardware), client job/scan operations (jobs are
created server-side until the M4 scanner), listener audio controls
(per-session, never global state).
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
RecordingId = Annotated[str, Field(pattern=r"^rec_[a-z0-9]{1,32}$")]
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
    end_recordings: bool = False        # confirm ending recordings the new capture cannot cover

    @model_validator(mode="after")
    def _changes_something(self) -> "ReceiverSetCaptureArgs":
        if self.center_hz is None and self.sample_rate_hz is None:
            raise ValueError("set_capture must change center_hz and/or sample_rate_hz")
        return self


class ReceiverSetAntennaArgs(Strict):
    receiver: ReceiverId
    port: PortId
    if_capture_gen: Gen | None = None


class ReceiverSetHwGainArgs(Strict):
    """Airspy HF+ hardware gain (M1). Only the given controls change; at least one."""
    receiver: ReceiverId
    hf_agc: bool | None = None
    hf_agc_threshold: Literal["low", "high"] | None = None    # acts with hf_agc on
    hf_lna: bool | None = None
    hf_att: Annotated[int, Field(strict=True, ge=0, le=8)] | None = None   # acts with hf_agc off

    @model_validator(mode="after")
    def _changes_something(self) -> "ReceiverSetHwGainArgs":
        if (self.hf_agc, self.hf_agc_threshold, self.hf_lna, self.hf_att) == (None, None, None, None):
            raise ValueError("receiver.set_hw_gain must change at least one control")
        return self


AgcName = Literal["off", "fast", "medium", "slow"]
# Passband edges relative to the dial frequency, integer Hz (USB 300..2700, LSB -2700..-300, ...).
EdgeHz = Annotated[int, Field(strict=True, ge=-100_000, le=100_000)]
ManualGainDb = Annotated[int, Field(strict=True, ge=0, le=100)]
Passband = Annotated[list[EdgeHz], Field(min_length=2, max_length=2)]   # [low, high]


def _check_passband(pb) -> None:
    if isinstance(pb, (list, tuple)) and not pb[0] < pb[1]:
        raise ValueError("passband_hz must be [low, high] with low < high")


class ChannelOpenArgs(Strict):
    receiver: ReceiverId
    frequency_hz: Hz        # absolute dial frequency; must lie within the current capture span
    mode: ModeId
    agc: AgcName = "medium"
    passband_hz: Passband | None = None    # None: the mode's default
    manual_gain_db: ManualGainDb = 0                    # fixed demodulator gain when agc is "off"

    @model_validator(mode="after")
    def _passband(self) -> "ChannelOpenArgs":
        _check_passband(self.passband_hz)
        return self


class ChannelTuneArgs(Strict):
    channel: ChannelId
    frequency_hz: Hz | None = None
    mode: ModeId | None = None
    agc: AgcName | None = None
    passband_hz: Passband | None = None
    passband_default: bool = False          # back to the mode's default passband (*_hz stays integer-only)
    manual_gain_db: ManualGainDb | None = None
    if_channel_gen: Gen | None = None

    @model_validator(mode="after")
    def _changes_something(self) -> "ChannelTuneArgs":
        if (self.frequency_hz, self.mode, self.agc, self.passband_hz, self.manual_gain_db) == (None,) * 5 \
                and not self.passband_default:
            raise ValueError("channel.tune must change at least one of frequency_hz, mode, agc, passband_hz, "
                             "passband_default, manual_gain_db")
        if self.passband_default and self.passband_hz is not None:
            raise ValueError("give passband_hz or passband_default, not both")
        _check_passband(self.passband_hz)
        return self


class ChannelCloseArgs(Strict):
    channel: ChannelId


class StreamSpectrumArgs(Strict):
    receiver: ReceiverId
    enabled: bool             # binary SPECTRUM messages for this receiver on this socket (stream v1)


class RecordingStartArgs(Strict):
    """Record clean demodulated audio (design §3.6) on a server-owned channel: it keeps recording
    when the browser is suspended or closed, until stopped or storage runs out."""
    receiver: ReceiverId
    frequency_hz: Hz
    mode: ModeId
    agc: AgcName = "medium"
    passband_hz: Passband | None = None
    manual_gain_db: ManualGainDb = 0

    @model_validator(mode="after")
    def _passband(self) -> "RecordingStartArgs":
        _check_passband(self.passband_hz)
        return self


class RecordingStopArgs(Strict):
    recording: RecordingId


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


class ReceiverSetHwGain(_Cmd):
    op: Literal["receiver.set_hw_gain"]
    args: ReceiverSetHwGainArgs


class ChannelOpen(_Cmd):
    op: Literal["channel.open"]
    args: ChannelOpenArgs


class ChannelTune(_Cmd):
    op: Literal["channel.tune"]
    args: ChannelTuneArgs


class ChannelClose(_Cmd):
    op: Literal["channel.close"]
    args: ChannelCloseArgs


class RecordingStart(_Cmd):
    op: Literal["recording.start"]
    args: RecordingStartArgs


class RecordingStop(_Cmd):
    op: Literal["recording.stop"]
    args: RecordingStopArgs


class StreamSpectrum(_Cmd):
    op: Literal["stream.spectrum"]
    args: StreamSpectrumArgs


ClientMessage = Annotated[
    Union[
        SessionHello, StateSubscribe, ReceiverClaim, ReceiverRelease,
        ReceiverSetCapture, ReceiverSetAntenna, ReceiverSetHwGain, ChannelOpen, ChannelTune, ChannelClose, StreamSpectrum,
        RecordingStart, RecordingStop,
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
    "receiver.set_hw_gain": "hw.retune",   # global device state like the capture: lease + hw.retune
    "channel.open": "listen",
    "channel.tune": "listen",
    "channel.close": "listen",
    "stream.spectrum": "listen",
    "recording.start": "record",
    "recording.stop": "record",
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


class FrequencyField(FieldState[int]):
    """A hardware-controlled frequency: requested and applied are integer Hz within the global bound."""

    requested: Hz | None = None
    applied: Hz | None = None


class SampleRateField(FieldState[int]):
    """A hardware-controlled sample rate: requested and applied are integer Hz, > 0, within u32."""

    requested: SampleRateHz | None = None
    applied: SampleRateHz | None = None


class HwGain(Strict):
    """Airspy HF+ gain controls as set in the device (no getters exist: verified stays "none")."""
    hf_agc: bool
    hf_agc_threshold: Literal["low", "high"]
    hf_lna: bool
    hf_att: Annotated[int, Field(strict=True, ge=0, le=8)]


class HwGainField(FieldState[HwGain]):
    requested: HwGain | None = None
    applied: HwGain | None = None


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
    center_hz: FrequencyField
    sample_rate_hz: SampleRateField
    antenna: FieldState[str]
    hw_gain: HwGainField = HwGainField(status="unsupported")   # supported by airspyhf receivers


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
    agc: AgcName = "medium"
    passband_hz: Passband | None = None     # the filter running (worker readback)
    passband_default: bool = True
    manual_gain_db: ManualGainDb = 0
    decoder_taps: list[ModeId] = []   # taps sit at demod output, upstream of all listener audio controls


class JobState(Strict):
    """A server-owned background job (e.g. a scan). Jobs never depend on a browser session."""
    kind: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,32}$")]
    receiver: ReceiverId
    state: Literal["running", "preempted", "stopped", "failed"]
    holds_lease: bool
    detail: ShortText | None = None
    since_utc: AwareDatetime


class ServerInfo(Strict):
    protocol: Literal[1]
    build: Annotated[str, Field(max_length=64)]
    exposure: Literal["loopback", "private_network", "public"]


class LedgerSummary(Strict):
    version: Annotated[int, Field(strict=True, ge=1)]
    available: list[ModeId] = []


class RecordingState(Strict):
    """An active recording (finished ones are listed over HTTP: /api/recordings)."""
    receiver: ReceiverId
    channel: ChannelId
    owner: Annotated[str, Field(max_length=128)]          # principal, e.g. "user:adam"
    frequency_hz: Hz
    mode: ModeId
    started_utc: Annotated[str, Field(max_length=40)]
    file: Annotated[str, Field(max_length=128)]            # <UTC>_<frequency_hz>_<mode>.wav
    seconds: Annotated[float, Field(ge=0)] = 0.0
    bytes: Annotated[int, Field(strict=True, ge=0)] = 0
    gaps: Annotated[int, Field(strict=True, ge=0)] = 0     # audio discontinuities inside the file


class StorageState(Strict):
    """Recording storage, visible to everyone who can record (B9: disk-full visible)."""
    used_bytes: Annotated[int, Field(strict=True, ge=0)] = 0
    cap_bytes: Annotated[int, Field(strict=True, ge=0)] = 0
    free_bytes: Annotated[int, Field(strict=True, ge=0)] = 0     # on the filesystem
    full: bool = False
    detail: ShortText | None = None


class ServerState(Strict):
    server: ServerInfo
    receivers: dict[ReceiverId, ReceiverState] = {}
    channels: dict[ChannelId, ChannelState] = {}
    jobs: dict[JobId, JobState] = {}
    recordings: dict[RecordingId, RecordingState] = {}
    storage: StorageState = StorageState()
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
    recording: RecordingId | None = None  # set by recording.start

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
