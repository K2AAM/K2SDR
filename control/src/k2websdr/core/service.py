"""ControlCore: sessions, command dispatch, leases, jobs and decoder intake.

Transport-free. Block 5 puts HTTP/WebSocket and authentication in front of
it; everything enforced here (schema, permissions, leases, preconditions,
ranges) therefore applies to every transport.

Per command the client receives exactly one `ack`; if accepted, exactly one
`result` follows. Immediate operations (claims, subscribe, ...) send both at
once. Retrying a command id within IDEMPOTENCY_TTL returns the original
outcome instead of executing it again.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from ..decoders.gate import GenerationGate
from ..protocol import models as m
from ..protocol.decoder import DecoderEvent
from ..protocol.errors import ErrorCode
from ..protocol import stream as sv
from ..protocol import worker_ctl as wc
from ..protocol.frame import FrameFlags, FrameHeader, FrameKind
from .lease import Holder, LeaseError
from .modes import WORKER_MODES, receiver_mode_problem, span_problem
from .recording import RecordingCatalog, WavWriter, file_name
from .receiver import WORKER_RESULT_TO_CODE, ReceiverConfig, ReceiverRuntime
from .store import StateStore

IDEMPOTENCY_TTL = 600.0
# Command capacity (see docs/protocol/control-v1.md, "Capacity"):
MAX_CMD_RECORDS = 10_000      # total idempotency records: in-flight + finished
MAX_IN_FLIGHT = 1_000         # accepted hardware commands not yet finished, all receivers
RECEIVER_QUEUE_LIMIT = 64     # per receiver: executing + queued hardware commands
# Listener channels (see docs/protocol/control-v1.md, "Channels"):
MAX_CHANNELS_PER_SESSION = 4
MAX_CHANNEL_CMDS_PER_SESSION = 8   # channel commands of one session waiting for the worker
_AGC = {"off": wc.Agc.OFF, "fast": wc.Agc.FAST, "medium": wc.Agc.MEDIUM, "slow": wc.Agc.SLOW}
# Browser stream v1 (see docs/protocol/stream-v1.md, "Backpressure"):
STREAM_AUDIO_QUEUE_BYTES = 96_000  # pending audio per session (~1 s for 4 channels at 24 kB/s)
MAX_RECORDINGS_PER_PRINCIPAL = 4
RECORDING_STATE_INTERVAL_S = 1.0   # progress and storage checks while recording


@dataclass(frozen=True)
class Principal:
    user: str                                   # e.g. "user:adam"
    permissions: frozenset[str]


log = logging.getLogger("k2websdr.core")


def _brief(cmd) -> str:
    """Command arguments for the log (no secrets travel over /ws commands)."""
    args = getattr(cmd, "args", None)
    return "" if args is None else str(args.model_dump(exclude_none=True, mode="json"))


class Session:
    _ids = itertools.count(1)

    def __init__(self, principal: Principal) -> None:
        self.id = f"s_{next(self._ids):06d}{int(time.time() * 1000) % 100000:05d}"
        self.principal = principal
        self.opened = time.monotonic()
        self.outbox: asyncio.Queue[m.BaseModel] = asyncio.Queue()
        self.subscribed = False
        self.closed = False
        # Binary stream (stream v1). Control messages always go first; spectrum keeps only the
        # newest row per receiver; audio is bounded and drops its oldest frames when full.
        self.wake = asyncio.Event()
        self._spectrum: OrderedDict[str, bytes] = OrderedDict()
        self._audio: deque[tuple[str, bytes]] = deque()
        self._audio_bytes = 0
        self._dropped_streams: set[str] = set()     # next frame of these streams gets SERVER_DROP
        self.stream_sent = Counter()                # kind name -> messages taken for sending
        self.stream_dropped = Counter()             # kind name -> messages discarded for backpressure

    @property
    def holder(self) -> Holder:
        return Holder.operator(self.principal.user, self.id)

    def send(self, msg: m.BaseModel) -> None:
        if not self.closed:
            m.dump_server_message(msg)          # validates before anything leaves the server
            self.outbox.put_nowait(msg)
            self.wake.set()

    # ----- binary stream
    def push_spectrum(self, receiver: str, msg: bytes) -> None:
        if self.closed:
            return
        if self._spectrum.pop(receiver, None) is not None:
            self.stream_dropped["spectrum"] += 1
            self._dropped_streams.add("r:" + receiver)
        self._spectrum[receiver] = self._flagged("r:" + receiver, msg)
        self.wake.set()

    def push_audio(self, channel: str, msg: bytes) -> None:
        if self.closed:
            return
        self._audio.append((channel, self._flagged("c:" + channel, msg)))
        self._audio_bytes += len(msg)
        while self._audio_bytes > STREAM_AUDIO_QUEUE_BYTES and len(self._audio) > 1:
            cid, old = self._audio.popleft()
            self._audio_bytes -= len(old)
            self.stream_dropped["audio"] += 1
            for i, (c, later) in enumerate(self._audio):     # flag the next queued frame of that channel
                if c == cid:
                    self._audio[i] = (c, sv.with_flags(later, sv.Flags.SERVER_DROP))
                    break
            else:
                self._dropped_streams.add("c:" + cid)
        self.wake.set()

    def _flagged(self, key: str, msg: bytes) -> bytes:
        if key in self._dropped_streams:
            self._dropped_streams.discard(key)
            return sv.with_flags(msg, sv.Flags.SERVER_DROP)
        return msg

    def has_binary(self) -> bool:
        return bool(self._audio or self._spectrum)

    def next_binary(self) -> bytes | None:
        """Next binary message to send: audio before spectrum (smaller, latency-sensitive)."""
        if self._audio:
            _, msg = self._audio.popleft()
            self._audio_bytes -= len(msg)
            self.stream_sent["audio"] += 1
            return msg
        if self._spectrum:
            _, msg = self._spectrum.popitem(last=False)
            self.stream_sent["spectrum"] += 1
            return msg
        return None

    def forget_stream(self, key: str) -> None:
        """Drop queued frames of a receiver ("r:<id>") or channel ("c:<id>") that is going away."""
        kind, ident = key.split(":", 1)
        if kind == "r":
            self._spectrum.pop(ident, None)
        else:
            kept = deque((c, b) for c, b in self._audio if c != ident)
            self._audio_bytes = sum(len(b) for _, b in kept)
            self._audio = kept
        self._dropped_streams.discard(key)


@dataclass
class _CmdRecord:
    ack: m.Ack
    result: m.Result | None = None
    sessions: set[str] = field(default_factory=set)
    created: float = field(default_factory=time.monotonic)
    finished: float | None = None   # set when the outcome is final: rejected ack, or result

    @property
    def in_flight(self) -> bool:
        return self.finished is None


@dataclass
class _Channel:
    """A listener channel. Present from acceptance of channel.open (so it counts against the
    limits); in the published state only once the worker has applied it."""
    id: str
    receiver: str
    session: str
    worker_id: int
    frequency_hz: int
    mode: str
    channel_gen: int = 0              # published (state, results, browser stream): never goes back
    worker_gen: int = 0               # the worker's own channel_gen: matches its audio frames
    gen_base: int = 0                 # channel_gen - worker_gen; grows when a new worker restarts at 1
    open: bool = False                # worker has applied it
    closing: bool = False
    agc: str = "medium"
    job: str | None = None                    # server-owned channel (a recording): no session
    passband: tuple[int, int] | None = None   # requested; None: the mode's default
    manual_gain_db: int = 0
    applied_passband: tuple[int, int] | None = None


@dataclass
class _Recording:
    id: str
    owner: str
    channel: _Channel
    writer: WavWriter
    started_utc: str
    file: str
    samples: int = 0
    gaps: int = 0
    last_update: float = 0.0
    started_audio: bool = False


class Rejected(Exception):
    def __init__(self, code: ErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code, self.detail = code, detail


class CapacityRejected(Rejected):
    """Transient overload. Not recorded: it cannot grow the cache, and a later retry
    of the same command id is evaluated afresh."""

    def __init__(self, detail: str) -> None:
        super().__init__(ErrorCode.E_BUSY, detail)


class ControlCore:
    def __init__(self, *, build: str = "dev", exposure: str = "loopback",
                 ledger: m.LedgerSummary | None = None, cmd_capacity: int | None = None,
                 in_flight_capacity: int | None = None, receiver_queue_limit: int | None = None,
                 recordings: RecordingCatalog | None = None) -> None:
        self.cmd_capacity = cmd_capacity or MAX_CMD_RECORDS
        # In-flight records are never evicted, so they must always leave room for at least one
        # finished record; then evicting finished records alone keeps the total bound.
        self.in_flight_capacity = in_flight_capacity or max(1, min(MAX_IN_FLIGHT, self.cmd_capacity - 1))
        self.receiver_queue_limit = receiver_queue_limit or RECEIVER_QUEUE_LIMIT
        if not 1 <= self.in_flight_capacity < self.cmd_capacity:
            raise ValueError("need 1 <= in_flight_capacity < cmd_capacity")
        if self.receiver_queue_limit < 1:
            raise ValueError("receiver_queue_limit must be >= 1")
        self._in_flight: set[tuple[str, str]] = set()
        initial = m.ServerState(server=m.ServerInfo(protocol=1, build=build, exposure=exposure),
                                ledger=ledger or m.LedgerSummary(version=1))
        self.store = StateStore(initial)
        self.gate = GenerationGate()
        self.receivers: dict[str, ReceiverRuntime] = {}
        self.sessions: dict[str, Session] = {}
        self._cmds: dict[tuple[str, str], _CmdRecord] = {}
        self._tasks: set[asyncio.Task] = set()
        self._job_ids = itertools.count(1)
        self._channels: dict[str, _Channel] = {}
        self._channel_ids = itertools.count(1)
        self._channel_cmds: dict[str, int] = {}          # session id -> channel commands in flight
        self._reconcile_locks: dict[str, asyncio.Lock] = {}
        self._spectrum_subs: dict[str, set[str]] = {}    # receiver id -> session ids (stream.spectrum)
        self.recordings = recordings
        self._recs: dict[str, _Recording] = {}
        self._rec_by_channel: dict[str, _Recording] = {}
        self._rec_ids = itertools.count(1)
        if recordings is not None:
            recordings.mark_unfinished_interrupted()
            self._publish_storage()
        self.stream_stale = Counter()                    # frames the server refused as stale/unowned
        self.decoder_sink: list[DecoderEvent] = []
        self.store.subscribe(self._broadcast)

    # ------------------------------------------------------------ receivers
    def add_receiver(self, cfg: ReceiverConfig, link_factory: Callable[[ReceiverRuntime], Any]) -> ReceiverRuntime:
        if cfg.receiver_id in self.receivers:
            raise ValueError(f"duplicate receiver {cfg.receiver_id}")
        rt = ReceiverRuntime(cfg, self.store, self.gate, link_factory)
        rt.lease.on_preempt(lambda prev, new, rid=cfg.receiver_id: self._on_preempt(rid, prev, new))
        rt.on_capture_change = lambda r, restarted: self._spawn(self._reconcile_channels(r, restarted))
        rt.frame_listeners.append(self._on_frame)
        with self.store.txn() as t:
            t.add("receivers", cfg.receiver_id, value=rt.initial_state())
        self.receivers[cfg.receiver_id] = rt
        return rt

    async def start(self) -> None:
        for rt in self.receivers.values():
            await rt.start()

    async def shutdown(self) -> None:
        for rec in list(self._recs.values()):
            self._finish_recording(rec, "interrupted", "service stopped")
        for rt in self.receivers.values():
            await rt.stop()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ------------------------------------------------------------- sessions
    def open_session(self, principal: Principal) -> Session:
        s = Session(principal)
        self.sessions[s.id] = s
        return s

    def close_session(self, s: Session) -> None:
        """Browser gone. Operator leases of this session end; jobs are untouched."""
        s.closed = True
        self.sessions.pop(s.id, None)
        log.info("session %s (%s) closed after %.0f s; stream sent %s, dropped for backpressure %s", s.id,
                 s.principal.user, time.monotonic() - s.opened, dict(s.stream_sent), dict(s.stream_dropped))
        for rt in self.receivers.values():
            if rt.lease.release_session(s.id):
                rt.publish_lease()
        for ch in [c for c in self._channels.values() if c.session == s.id]:
            self._drop_channel(ch, tell_worker=True)
        for subs in self._spectrum_subs.values():
            subs.discard(s.id)

    def _broadcast(self, patch: m.Patch) -> None:
        for s in list(self.sessions.values()):
            if s.subscribed:
                s.send(patch)

    # ------------------------------------------------------------- commands
    async def handle(self, s: Session, raw: str | bytes) -> None:
        """Process one inbound message. Never raises for client input."""
        try:
            cmd = m.parse_client_message(raw)
        except m.ProtocolError as e:
            if e.cmd_id and _is_ulid(e.cmd_id):
                s.send(m.Ack(v=1, type="ack", id=e.cmd_id, status="rejected", code=e.code, detail=e.detail))
            else:
                s.send(m.ErrorMsg(v=1, type="error", code=e.code, detail=e.detail))
            return
        self._expire_cmds()
        key = (s.principal.user, cmd.id)
        rec = self._cmds.get(key)
        if rec is not None:                     # idempotent retry, possibly from a new session
            rec.sessions.add(s.id)
            s.send(rec.ack)
            if rec.result is not None:
                s.send(rec.result)
            return
        log.info("cmd %s %s %s %s", s.id, cmd.id[-6:], cmd.op, _brief(cmd))
        try:
            needed = m.OP_PERMISSION[cmd.op]
            if needed is not None and needed not in s.principal.permissions:
                raise Rejected(ErrorCode.E_FORBIDDEN, f"{cmd.op} requires permission {needed}")
            await getattr(self, "_op_" + cmd.op.replace(".", "_"))(s, cmd)
        except CapacityRejected as r:
            log.info("cmd %s rejected %s: %s", cmd.id[-6:], r.code, r.detail)
            s.send(m.Ack(v=1, type="ack", id=cmd.id, status="rejected", code=r.code, detail=r.detail))
        except Rejected as r:
            log.info("cmd %s rejected %s: %s", cmd.id[-6:], r.code, r.detail)
            self._reply_ack(s, cmd.id, "rejected", code=r.code, detail=r.detail)
        except LeaseError as le:
            log.info("cmd %s rejected %s: %s", cmd.id[-6:], le.code, le.detail)
            self._reply_ack(s, cmd.id, "rejected", code=le.code, detail=le.detail)

    def _reply_ack(self, s: Session, cmd_id: str, status: str, *, code: ErrorCode | None = None,
                   detail: str | None = None) -> _CmdRecord:
        ack = m.Ack(v=1, type="ack", id=cmd_id, status=status, code=code, detail=detail,
                    rev=self.store.rev if status == "accepted" else None)
        rec = _CmdRecord(ack=ack, sessions={s.id})
        if status == "rejected":
            rec.finished = time.monotonic()      # a rejection is a final outcome
        self._cmds[(s.principal.user, cmd_id)] = rec
        self._expire_cmds()                      # keep the bound after every insertion
        s.send(ack)
        return rec

    def _reply_result(self, rec: _CmdRecord, cmd_id: str, status: str, *, code: ErrorCode | None = None,
                      detail: str | None = None, capture_gen: int | None = None, channel: str | None = None,
                      channel_gen: int | None = None, recording: str | None = None) -> None:
        rec.result = m.Result(v=1, type="result", id=cmd_id, status=status, code=code, detail=detail,
                              rev=self.store.rev, capture_gen=capture_gen, channel=channel,
                              channel_gen=channel_gen, recording=recording)
        rec.finished = time.monotonic()
        if status != "applied":
            log.info("cmd %s %s %s: %s", cmd_id[-6:], status, code, detail)
        for sid in rec.sessions:
            sess = self.sessions.get(sid)
            if sess is not None:
                sess.send(rec.result)

    def _immediate(self, s: Session, cmd_id: str) -> None:
        rec = self._reply_ack(s, cmd_id, "accepted")
        self._reply_result(rec, cmd_id, "applied")

    def _expire_cmds(self) -> None:
        """Drop finished outcomes older than the TTL, then enforce the hard bound.

        In-flight commands (accepted, no result yet) are never dropped: a retry
        of one must keep finding it, or the command could execute twice.
        """
        cutoff = time.monotonic() - IDEMPOTENCY_TTL
        for k in [k for k, r in self._cmds.items() if not r.in_flight and r.finished < cutoff]:
            del self._cmds[k]
        excess = len(self._cmds) - self.cmd_capacity
        if excess > 0:
            finished = sorted((r.finished, k) for k, r in self._cmds.items() if not r.in_flight)
            for _, k in finished[:excess]:
                del self._cmds[k]

    def _receiver(self, rid: str) -> ReceiverRuntime:
        rt = self.receivers.get(rid)
        if rt is None:
            raise Rejected(ErrorCode.E_UNKNOWN_RECEIVER, f"no receiver {rid}")
        return rt

    # ----- operations
    async def _op_session_hello(self, s: Session, cmd: m.SessionHello) -> None:
        self._immediate(s, cmd.id)

    async def _op_state_subscribe(self, s: Session, cmd: m.StateSubscribe) -> None:
        rec = self._reply_ack(s, cmd.id, "accepted")
        s.send(self.store.snapshot())
        s.subscribed = True                     # no await between snapshot and flag: no patch is missed
        self._reply_result(rec, cmd.id, "applied")

    async def _op_receiver_claim(self, s: Session, cmd: m.ReceiverClaim) -> None:
        rt = self._receiver(cmd.args.receiver)
        rt.lease.claim(s.holder, cmd.args.takeover)
        rt.publish_lease()
        self._immediate(s, cmd.id)

    async def _op_receiver_release(self, s: Session, cmd: m.ReceiverRelease) -> None:
        rt = self._receiver(cmd.args.receiver)
        rt.lease.release(s.holder)
        rt.publish_lease()
        self._immediate(s, cmd.id)

    async def _op_receiver_set_capture(self, s: Session, cmd: m.ReceiverSetCapture) -> None:
        a = cmd.args
        rt = self._receiver(a.receiver)
        rt.lease.require(s.holder)
        state = self.store.view()["receivers"][a.receiver]
        if state["acquisition"]["state"] != "running":
            raise Rejected(ErrorCode.E_DEVICE_FAILED, f"{a.receiver} acquisition is {state['acquisition']['state']}")
        if a.if_capture_gen is not None and a.if_capture_gen != rt.capture_gen:
            raise Rejected(ErrorCode.E_STALE_PRECONDITION,
                           f"capture_gen is {rt.capture_gen}, not {a.if_capture_gen}")
        caps = state["capabilities"]
        if a.center_hz is not None:
            rng = caps["freq_range_hz"]
            if rng is None:
                raise Rejected(ErrorCode.E_UNSUPPORTED, f"{a.receiver} has no tunable center")
            if not rng[0] <= a.center_hz <= rng[1]:
                raise Rejected(ErrorCode.E_OUT_OF_RANGE, f"center_hz outside {rng[0]}..{rng[1]}")
        if a.sample_rate_hz is not None and a.sample_rate_hz not in caps["sample_rates_hz"]:
            raise Rejected(ErrorCode.E_OUT_OF_RANGE, f"sample_rate_hz not in {caps['sample_rates_hz']}")
        # A retune that would leave an active recording outside the capture ends it: say so first
        # and let the user confirm (Adam, 2026-10-28: warn, with an option to end it).
        cut = self._recordings_cut_by(rt, a.center_hz, a.sample_rate_hz)
        if cut and not a.end_recordings:
            raise Rejected(ErrorCode.E_RECORDING_ACTIVE,
                           f"this retune would end {len(cut)} active recording(s): {self._rec_names(cut)}; "
                           "send end_recordings to end them and retune")
        if cut:
            log.info("cmd %s: %s confirmed ending %s", cmd.id[-6:], s.principal.user, [c.id for c in cut])
        confirmed = {c.id for c in cut}
        base_check = self._authorizer(s, rt, "hw.retune", a.if_capture_gen)

        def check() -> tuple[ErrorCode, str] | None:
            # Re-checked when the retune reaches the front of the queue: a recording that started
            # while it waited was not part of the confirmation and must not be ended silently.
            denied = base_check()
            if denied:
                return denied
            new = [c for c in self._recordings_cut_by(rt, a.center_hz, a.sample_rate_hz) if c.id not in confirmed]
            if new:
                return (ErrorCode.E_RECORDING_ACTIVE,
                        f"a recording started while this retune waited and would end: {self._rec_names(new)}; "
                        "send end_recordings to end it and retune")
            return None

        self._check_capacity(rt)                  # before anything is marked pending or queued
        _rev, fut = rt.submit_set_capture(cmd.id, a.center_hz, a.sample_rate_hz, authorize=check)
        key = (s.principal.user, cmd.id)
        self._in_flight.add(key)
        rec = self._reply_ack(s, cmd.id, "accepted")
        self._spawn(self._finish(rec, key, fut))

    async def _op_receiver_set_hw_gain(self, s: Session, cmd: m.ReceiverSetHwGain) -> None:
        """Hardware gain is global device state: lease + hw.retune, serialized with retunes."""
        a = cmd.args
        rt = self._receiver(a.receiver)
        rt.lease.require(s.holder)
        state = self.store.view()["receivers"][a.receiver]
        field = state["capture"]["hw_gain"]
        if field["status"] == "unsupported":
            raise Rejected(ErrorCode.E_UNSUPPORTED, f"{a.receiver} has no hardware gain controls")
        if state["acquisition"]["state"] != "running":
            raise Rejected(ErrorCode.E_DEVICE_FAILED, f"{a.receiver} acquisition is {state['acquisition']['state']}")
        # Start from the newest wish (a pending request), else what is applied: commands compose.
        base = field["requested"] if field["status"] == "pending" else field["applied"]
        if base is None:
            raise Rejected(ErrorCode.E_DEVICE_FAILED, f"{a.receiver} has not reported its gain yet")
        new = {k: v for k, v in (("hf_agc", a.hf_agc), ("hf_agc_threshold", a.hf_agc_threshold),
                                 ("hf_lna", a.hf_lna), ("hf_att", a.hf_att)) if v is not None}
        gain = m.HwGain(**{**base, **new})
        self._check_capacity(rt)
        _rev, fut = rt.submit_set_hw_gain(cmd.id, gain, authorize=self._authorizer(s, rt, "hw.retune", None))
        key = (s.principal.user, cmd.id)
        self._in_flight.add(key)
        rec = self._reply_ack(s, cmd.id, "accepted")
        self._spawn(self._finish(rec, key, fut))

    def _check_capacity(self, rt: ReceiverRuntime) -> None:
        if len(self._in_flight) >= self.in_flight_capacity:
            raise CapacityRejected(f"server busy: {len(self._in_flight)} hardware commands in progress "
                                   f"(limit {self.in_flight_capacity}); retry after some complete")
        if rt.outstanding >= self.receiver_queue_limit:
            raise CapacityRejected(f"{rt.id} busy: {rt.outstanding} commands queued or executing "
                                   f"(limit {self.receiver_queue_limit}); retry after some complete")

    def _authorizer(self, s: Session, rt: ReceiverRuntime, permission: str, if_capture_gen: int | None):
        """Everything checked at acceptance, re-checked when the command reaches the front of the queue."""
        holder = s.holder

        def check() -> tuple[ErrorCode, str] | None:
            if s.closed or self.sessions.get(s.id) is not s:
                return ErrorCode.E_UNAUTHENTICATED, "session ended before the command ran"
            if permission not in s.principal.permissions:
                return ErrorCode.E_FORBIDDEN, f"requires permission {permission}"
            if rt.lease.holder != holder:
                who = rt.lease.holder.principal if rt.lease.holder else "nobody"
                return ErrorCode.E_NOT_LEASE_HOLDER, f"lease lost before the command ran (now held by {who})"
            if if_capture_gen is not None and rt.capture_gen != if_capture_gen:
                return ErrorCode.E_STALE_PRECONDITION, f"capture_gen is {rt.capture_gen}, not {if_capture_gen}"
            return None

        return check

    async def _finish(self, rec: _CmdRecord, key: tuple[str, str], fut: asyncio.Future) -> None:
        try:
            out = await fut
            self._reply_result(rec, key[1], out.status, code=out.code, detail=out.detail,
                               capture_gen=out.capture_gen)
        finally:
            self._in_flight.discard(key)
            self._expire_cmds()

    async def _op_receiver_set_antenna(self, s: Session, cmd: m.ReceiverSetAntenna) -> None:
        rt = self._receiver(cmd.args.receiver)
        rt.lease.require(s.holder)
        ports = self.store.view()["receivers"][cmd.args.receiver]["capabilities"]["antennas"]
        if not ports:
            raise Rejected(ErrorCode.E_UNSUPPORTED, f"{cmd.args.receiver} has no antenna switch")
        # No worker message for antennas exists before M2 hardware work.
        raise Rejected(ErrorCode.E_UNSUPPORTED, "antenna switching is not implemented before M2")

    # ----- listener channels
    def _capture_now(self, rt: ReceiverRuntime) -> tuple[int | None, int | None, bool]:
        """(applied center, applied rate, complex IQ) of a receiver."""
        st = self.store.view()["receivers"][rt.id]
        return (st["capture"]["center_hz"]["applied"], st["capture"]["sample_rate_hz"]["applied"],
                st["capabilities"]["sample_format"] in ("ci16", "cf32"))

    def _check_channel_fits(self, rt: ReceiverRuntime, mode: str, frequency_hz: int,
                            passband: tuple[int, int] | None = None) -> int:
        """Mode availability (ledger + this receiver) and span. Returns the offset from the centre."""
        if mode not in self.store.view()["ledger"]["available"]:
            raise Rejected(ErrorCode.E_MODE_UNAVAILABLE, f"mode {mode} is not available")
        if self.store.view()["receivers"][rt.id]["acquisition"]["state"] != "running" or rt.link is None:
            raise Rejected(ErrorCode.E_DEVICE_FAILED, f"{rt.id} is not running")
        center, rate, complex_iq = self._capture_now(rt)
        problem = receiver_mode_problem(mode, rate, complex_iq)
        if problem:
            raise Rejected(ErrorCode.E_MODE_UNAVAILABLE, problem)
        if center is None:
            raise Rejected(ErrorCode.E_DEVICE_FAILED, f"{rt.id} has no applied centre frequency yet")
        problem = span_problem(mode, frequency_hz, center, rate, passband)
        if problem:
            raise Rejected(ErrorCode.E_OUT_OF_RANGE, problem)
        return frequency_hz - center

    @staticmethod
    def _cannot_keep(mode: str, frequency_hz: int, passband, center: int | None, rate: int | None,
                     complex_iq: bool) -> bool:
        return center is None or rate is None or bool(receiver_mode_problem(mode, rate, complex_iq)) or \
            bool(span_problem(mode, frequency_hz, center, rate, passband))

    def _recordings_cut_by(self, rt: ReceiverRuntime, center_hz: int | None,
                           rate_hz: int | None) -> list[_Channel]:
        """Recording channels on this receiver (active, or still opening) that a capture of
        (center, rate) could not keep."""
        center0, rate0, complex_iq = self._capture_now(rt)
        center = center_hz if center_hz is not None else center0
        rate = rate_hz if rate_hz is not None else rate0
        return [ch for ch in self._channels.values()
                if ch.job and ch.receiver == rt.id and not ch.closing
                and self._cannot_keep(ch.mode, ch.frequency_hz, ch.passband, center, rate, complex_iq)]

    def _rec_names(self, chans: list[_Channel]) -> str:
        out = []
        for ch in chans:
            r = self._rec_by_channel.get(ch.id)
            who = r.owner if r else "starting"
            out.append(f"{r.id if r else ch.id} ({ch.frequency_hz} Hz {ch.mode}, {who})")
        return ", ".join(out)

    @staticmethod
    def _take_gen(ch: _Channel, worker_gen: int, *, new_worker: bool = False) -> None:
        """Map the worker's channel_gen to the published one. A restarted worker (device unplugged
        and back) numbers channels from 1 again; the published channel_gen must still increase, or
        every browser drops the new audio as stale (Window 3, 2026-10-28)."""
        pub = worker_gen + ch.gen_base
        if (new_worker or worker_gen != ch.worker_gen) and pub <= ch.channel_gen:
            ch.gen_base += ch.channel_gen + 1 - pub
            pub = ch.channel_gen + 1
        ch.worker_gen, ch.channel_gen = worker_gen, pub

    @staticmethod
    def _worker_channel(ch: _Channel, offset_hz: int) -> wc.Channel:
        # CW pitch (BFO) stays at the worker default (700 Hz).
        lo, hi = ch.passband if ch.passband is not None else (None, None)
        return wc.Channel(ch.worker_id, WORKER_MODES[ch.mode].worker, _AGC[ch.agc], offset_hz, lo, hi, None,
                          ch.manual_gain_db * 100)

    def _owned_channel(self, s: Session, cid: str) -> _Channel:
        ch = self._channels.get(cid)
        if ch is None or ch.closing:
            raise Rejected(ErrorCode.E_UNKNOWN_CHANNEL, f"no channel {cid}")
        if ch.session != s.id:
            raise Rejected(ErrorCode.E_FORBIDDEN, f"{cid} belongs to another session")
        if not ch.open:
            raise Rejected(ErrorCode.E_BUSY, f"{cid} is still opening")
        return ch

    def _reserve_channel_cmd(self, s: Session) -> None:
        n = self._channel_cmds.get(s.id, 0)
        if n >= MAX_CHANNEL_CMDS_PER_SESSION:
            raise CapacityRejected(f"{n} channel commands of this session are still in progress "
                                   f"(limit {MAX_CHANNEL_CMDS_PER_SESSION}); retry after some complete")
        self._channel_cmds[s.id] = n + 1

    def _release_channel_cmd(self, sid: str) -> None:
        n = self._channel_cmds.get(sid, 0) - 1
        if n > 0:
            self._channel_cmds[sid] = n
        else:
            self._channel_cmds.pop(sid, None)

    @staticmethod
    def _applied_passband(mode: str, ap: wc.ChannelApplied) -> tuple[int, int]:
        """The filter the worker runs (CHANNEL_APPLIED readback; worker-dsp-v1)."""
        lo, hi = ap.channel.pass_lo_hz, ap.channel.pass_hi_hz
        if lo is None or hi is None:
            wm = WORKER_MODES[mode]
            return wm.pass_lo_hz, wm.pass_hi_hz
        return lo, hi

    def _publish_channel(self, ch: _Channel) -> None:
        with self.store.txn() as t:
            owner = m.ChannelOwner(job=ch.job) if ch.job else m.ChannelOwner(session=ch.session)
            value = m.ChannelState(receiver=ch.receiver, owner=owner,
                                   frequency_hz=ch.frequency_hz, mode=ch.mode, channel_gen=ch.channel_gen,
                                   agc=ch.agc, passband_default=ch.passband is None,
                                   passband_hz=list(ch.applied_passband) if ch.applied_passband else None,
                                   manual_gain_db=ch.manual_gain_db)
            if ch.id in self.store.view()["channels"]:
                t.replace("channels", ch.id, value=value)
            else:
                t.add("channels", ch.id, value=value)

    def _drop_channel(self, ch: _Channel, *, tell_worker: bool) -> None:
        """Remove from registry and state now; optionally tell the worker (best effort)."""
        ch.closing = True
        self._channels.pop(ch.id, None)
        log.info("channel %s (%s, %d Hz %s) closed", ch.id, ch.session or ch.job, ch.frequency_hz, ch.mode)
        rec = self._rec_by_channel.get(ch.id)
        if rec is not None:                                # e.g. no longer fits after a receiver retune
            self._finish_recording(rec, "interrupted", "the receiver no longer covers this frequency",
                                   drop_channel=False)
        owner = self.sessions.get(ch.session)
        if owner is not None:
            owner.forget_stream("c:" + ch.id)               # nothing of a closed channel is sent
        if ch.id in self.store.view()["channels"]:
            with self.store.txn() as t:
                t.remove("channels", ch.id)
        rt = self.receivers.get(ch.receiver)
        if tell_worker and ch.open and rt is not None and rt.link is not None:
            self._spawn(self._quiet(rt.link.channel_close(ch.worker_id, rt.cfg.command_timeout)))

    @staticmethod
    async def _quiet(coro) -> None:
        from ..devices.worker_client import WorkerError
        try:
            await coro
        except WorkerError:
            pass

    async def _op_channel_open(self, s: Session, cmd: m.ChannelOpen) -> None:
        a = cmd.args
        rt = self._receiver(a.receiver)
        passband = tuple(a.passband_hz) if a.passband_hz is not None else None
        offset = self._check_channel_fits(rt, a.mode, a.frequency_hz, passband)
        mine = sum(1 for c in self._channels.values() if c.session == s.id)
        if mine >= MAX_CHANNELS_PER_SESSION:
            raise Rejected(ErrorCode.E_BUSY, f"this session already has {mine} channels "
                                             f"(limit {MAX_CHANNELS_PER_SESSION})")
        on_rx = sum(1 for c in self._channels.values() if c.receiver == rt.id)
        if on_rx >= wc.MAX_CHANNELS:
            raise Rejected(ErrorCode.E_BUSY, f"{rt.id} already has {on_rx} channels (limit {wc.MAX_CHANNELS})")
        self._reserve_channel_cmd(s)
        n = next(self._channel_ids)
        ch = _Channel(id=f"ch_{n}", receiver=rt.id, session=s.id, worker_id=n,
                      frequency_hz=a.frequency_hz, mode=a.mode, agc=a.agc, passband=passband,
                      manual_gain_db=a.manual_gain_db)
        self._channels[ch.id] = ch
        rec = self._reply_ack(s, cmd.id, "accepted")
        self._spawn(self._finish_channel(s, rec, cmd.id, rt, ch, "open", rt.link.channel_open(
            self._worker_channel(ch, offset), rt.cfg.command_timeout)))

    async def _op_channel_tune(self, s: Session, cmd: m.ChannelTune) -> None:
        a = cmd.args
        ch = self._owned_channel(s, a.channel)
        if a.if_channel_gen is not None and a.if_channel_gen != ch.channel_gen:
            raise Rejected(ErrorCode.E_STALE_PRECONDITION, f"channel_gen is {ch.channel_gen}, not {a.if_channel_gen}")
        rt = self._receiver(ch.receiver)
        mode = a.mode if a.mode is not None else ch.mode
        freq = a.frequency_hz if a.frequency_hz is not None else ch.frequency_hz
        # A custom passband belongs to its mode (USB 300..2700 is not an LSB filter): a mode change
        # returns to the new mode's default unless the same command gives one.
        if a.passband_default:
            passband = None
        elif a.passband_hz is not None:
            passband = tuple(a.passband_hz)
        else:
            passband = ch.passband if mode == ch.mode else None
        offset = self._check_channel_fits(rt, mode, freq, passband)
        self._reserve_channel_cmd(s)
        target = _Channel(id=ch.id, receiver=ch.receiver, session=ch.session, worker_id=ch.worker_id,
                          frequency_hz=freq, mode=mode, agc=a.agc if a.agc is not None else ch.agc,
                          passband=passband,
                          manual_gain_db=a.manual_gain_db if a.manual_gain_db is not None else ch.manual_gain_db)
        rec = self._reply_ack(s, cmd.id, "accepted")
        self._spawn(self._finish_channel(s, rec, cmd.id, rt, target, "tune", rt.link.channel_tune(
            self._worker_channel(target, offset), rt.cfg.command_timeout)))

    async def _op_channel_close(self, s: Session, cmd: m.ChannelClose) -> None:
        ch = self._owned_channel(s, cmd.args.channel)
        rt = self._receiver(ch.receiver)
        self._reserve_channel_cmd(s)
        self._drop_channel(ch, tell_worker=False)
        rec = self._reply_ack(s, cmd.id, "accepted")
        link = rt.link
        coro = link.channel_close(ch.worker_id, rt.cfg.command_timeout) if link is not None else None
        self._spawn(self._finish_channel(s, rec, cmd.id, rt, ch, "close", coro))

    # ----- browser stream (stream v1)
    async def _op_stream_spectrum(self, s: Session, cmd: m.StreamSpectrum) -> None:
        rt = self._receiver(cmd.args.receiver)
        subs = self._spectrum_subs.setdefault(rt.id, set())
        if cmd.args.enabled:
            subs.add(s.id)
        else:
            subs.discard(s.id)
            s.forget_stream("r:" + rt.id)
        self._immediate(s, cmd.id)

    def _on_frame(self, rid: str, hdr: FrameHeader, payload: bytes) -> None:
        """Worker frame -> browser stream. Stale generations never leave the server (outline §13)."""
        rt = self.receivers.get(rid)
        if rt is None:
            return
        if hdr.kind is FrameKind.SPECTRUM:
            subs = self._spectrum_subs.get(rid)
            if not subs:
                return
            if hdr.capture_gen != rt.capture_gen:
                self.stream_stale["spectrum"] += 1
                return
            se = hdr.spectrum_ext
            msg = sv.pack(sv.Kind.SPECTRUM, _stream_flags(hdr),
                          hdr.capture_gen, hdr.first_sample_index, hdr.freq_origin_hz, hdr.sample_rate_hz, rid,
                          sv.SpectrumInfo(se.db_floor_mdb, se.db_step_mdb, se.blocks), payload)
            for sid in list(subs):
                sess = self.sessions.get(sid)
                if sess is not None:
                    sess.push_spectrum(rid, msg)
        elif hdr.kind is FrameKind.AUDIO:
            ae = hdr.audio_ext
            ch = next((c for c in self._channels.values()
                       if c.worker_id == hdr.stream_id and c.receiver == rid), None)
            if (ch is None or not ch.open or ch.closing or ae.channel_gen != ch.worker_gen
                    or hdr.capture_gen != rt.capture_gen):
                self.stream_stale["audio"] += 1
                return
            rec = self._rec_by_channel.get(ch.id)
            if rec is not None:
                self._record_audio(rec, hdr, payload)
                return
            sess = self.sessions.get(ch.session)
            if sess is None:
                return
            sess.push_audio(ch.id, sv.pack(
                sv.Kind.AUDIO, _stream_flags(hdr), ch.channel_gen,
                hdr.first_sample_index, hdr.freq_origin_hz, hdr.sample_rate_hz, ch.id,
                sv.AudioInfo(ae.level_cdb, ae.pass_lo_hz, ae.pass_hi_hz, int(ae.mode), int(ae.agc)), payload))

    # ----- recordings (design §3.6)
    def refresh_storage(self) -> None:
        """Re-publish the storage summary (after a deletion over HTTP)."""
        self._publish_storage()

    def _publish_storage(self) -> None:
        cat = self.recordings
        if cat is None:
            return
        reason = cat.full_reason()
        value = m.StorageState(used_bytes=cat.used_bytes(), cap_bytes=cat.policy.cap_bytes,
                               free_bytes=cat.free_bytes(), full=reason is not None, detail=reason)
        with self.store.txn() as t:
            t.replace("storage", value=value)

    def _publish_recording(self, rec: _Recording) -> None:
        ch = rec.channel
        value = m.RecordingState(receiver=ch.receiver, channel=ch.id, owner=rec.owner, frequency_hz=ch.frequency_hz,
                                 mode=ch.mode, started_utc=rec.started_utc, file=rec.file,
                                 seconds=round(rec.samples / 12000, 1), bytes=rec.writer.file_bytes, gaps=rec.gaps)
        with self.store.txn() as t:
            if rec.id in self.store.view()["recordings"]:
                t.replace("recordings", rec.id, value=value)
            else:
                t.add("recordings", rec.id, value=value)

    async def _op_recording_start(self, s: Session, cmd: m.RecordingStart) -> None:
        a = cmd.args
        if self.recordings is None:
            raise Rejected(ErrorCode.E_UNSUPPORTED, "recording is not configured on this server")
        reason = self.recordings.full_reason()
        if reason:
            self._publish_storage()
            raise Rejected(ErrorCode.E_STORAGE_FULL, reason)
        rt = self._receiver(a.receiver)
        passband = tuple(a.passband_hz) if a.passband_hz is not None else None
        offset = self._check_channel_fits(rt, a.mode, a.frequency_hz, passband)
        # A retune already queued or running that this recording would not survive: refuse now,
        # rather than start a recording the retune then ends without anyone confirming it.
        center, rate, complex_iq = self._capture_now(rt)
        for c, r in rt.pending_captures:
            center, rate = (c if c is not None else center), (r if r is not None else rate)
            if self._cannot_keep(a.mode, a.frequency_hz, passband, center, rate, complex_iq):
                raise Rejected(ErrorCode.E_BUSY, f"a pending retune (centre {center} Hz, {rate} S/s) would not "
                                                 f"cover {a.frequency_hz} Hz; start the recording after it completes")
        mine = sum(1 for r in self._recs.values() if r.owner == s.principal.user)
        if mine >= MAX_RECORDINGS_PER_PRINCIPAL:
            raise Rejected(ErrorCode.E_BUSY, f"already {mine} recordings running (limit {MAX_RECORDINGS_PER_PRINCIPAL})")
        on_rx = sum(1 for c in self._channels.values() if c.receiver == rt.id)
        if on_rx >= wc.MAX_CHANNELS:
            raise Rejected(ErrorCode.E_BUSY, f"{rt.id} already has {on_rx} channels (limit {wc.MAX_CHANNELS})")
        self._reserve_channel_cmd(s)
        n = next(self._channel_ids)
        rid = f"rec_{next(self._rec_ids)}{int(time.time()) % 100000}"
        ch = _Channel(id=f"ch_{n}", receiver=rt.id, session="", worker_id=n, frequency_hz=a.frequency_hz, mode=a.mode,
                      agc=a.agc, passband=passband, manual_gain_db=a.manual_gain_db,
                      job=f"job_{rid.replace('_', '-')}")
        self._channels[ch.id] = ch
        rec = self._reply_ack(s, cmd.id, "accepted")
        self._spawn(self._start_recording(s, rec, cmd.id, rt, ch, rid, rt.link.channel_open(
            self._worker_channel(ch, offset), rt.cfg.command_timeout)))

    async def _start_recording(self, s: Session, rec_cmd: _CmdRecord, cmd_id: str, rt: ReceiverRuntime, ch: _Channel,
                               rid: str, coro) -> None:
        from ..devices.worker_client import WorkerFailed, WorkerGone, WorkerRejected, WorkerTimeout
        try:
            try:
                ap = await coro
            except (WorkerRejected, WorkerFailed) as e:
                self._channels.pop(ch.id, None)
                self._reply_result(rec_cmd, cmd_id, "failed", code=WORKER_RESULT_TO_CODE.get(e.result, ErrorCode.E_INTERNAL),
                                   detail=f"worker refused: {e.result.name}")
                return
            except (WorkerTimeout, WorkerGone):
                self._channels.pop(ch.id, None)
                self._reply_result(rec_cmd, cmd_id, "failed", code=ErrorCode.E_DEVICE_FAILED,
                                   detail="receiver worker did not open the recording channel")
                return
            self._take_gen(ch, ap.channel_gen)
            ch.open = True
            ch.applied_passband = self._applied_passband(ch.mode, ap)
            started = datetime.now(timezone.utc)
            path = self.recordings.unique_path(file_name(started, ch.frequency_hz, ch.mode))
            try:
                writer = WavWriter(path)
                self.recordings.insert(rid, s.principal.user, ch.receiver, ch.frequency_hz, ch.mode,
                                       started.isoformat(), path.name)
            except OSError as e:
                self._drop_channel(ch, tell_worker=True)
                self._reply_result(rec_cmd, cmd_id, "failed", code=ErrorCode.E_STORAGE_FULL,
                                   detail=f"cannot create the recording file: {e.strerror}")
                return
            r = _Recording(id=rid, owner=s.principal.user, channel=ch, writer=writer, started_utc=started.isoformat(),
                           file=path.name, last_update=time.monotonic())
            self._recs[rid] = r
            self._rec_by_channel[ch.id] = r
            self._publish_channel(ch)
            self._publish_recording(r)
            self._publish_storage()
            self._reply_result(rec_cmd, cmd_id, "applied", channel=ch.id, channel_gen=ch.channel_gen, recording=rid)
        finally:
            self._release_channel_cmd(s.id)
            self._expire_cmds()

    def _record_audio(self, rec: _Recording, hdr: FrameHeader, payload: bytes) -> None:
        if rec.started_audio and _stream_flags(hdr) & sv.Flags.DISCONTINUITY:
            rec.gaps += 1
        rec.started_audio = True
        try:
            rec.writer.write(payload)
        except OSError as e:
            self._finish_recording(rec, "failed", f"write failed: {e.strerror}")
            return
        rec.samples += len(payload) // 2
        now = time.monotonic()
        if now - rec.last_update >= RECORDING_STATE_INTERVAL_S:
            rec.last_update = now
            self.recordings.update(rec.id, bytes_=rec.writer.file_bytes, samples=rec.samples, gaps=rec.gaps)
            reason = self.recordings.full_reason()
            if reason:
                self._finish_recording(rec, "storage_full", reason)
                return
            self._publish_recording(rec)
            self._publish_storage()

    def _finish_recording(self, rec: _Recording, status: str, detail: str | None = None, *,
                          drop_channel: bool = True) -> None:
        """Close the file and the catalog row, remove the recording from the state, free its channel."""
        if self._recs.pop(rec.id, None) is None:
            return
        self._rec_by_channel.pop(rec.channel.id, None)
        try:
            rec.writer.close()
        except OSError:
            status, detail = "failed", "could not finalize the file"
        self.recordings.update(rec.id, bytes_=rec.writer.file_bytes, samples=rec.samples, gaps=rec.gaps, status=status,
                               detail=detail, ended=True)
        if rec.id in self.store.view()["recordings"]:
            with self.store.txn() as t:
                t.remove("recordings", rec.id)
        if drop_channel:
            self._drop_channel(rec.channel, tell_worker=True)
        self._publish_storage()

    async def _op_recording_stop(self, s: Session, cmd: m.RecordingStop) -> None:
        rec = self._recs.get(cmd.args.recording)
        if rec is None:
            raise Rejected(ErrorCode.E_UNKNOWN_RECORDING, f"no active recording {cmd.args.recording}")
        if rec.owner != s.principal.user and "admin" not in s.principal.permissions:
            raise Rejected(ErrorCode.E_FORBIDDEN, "only the user who started a recording (or an admin) can stop it")
        self._finish_recording(rec, "done")
        rec_cmd = self._reply_ack(s, cmd.id, "accepted")
        self._reply_result(rec_cmd, cmd.id, "applied", recording=rec.id)

    async def _finish_channel(self, s: Session, rec: _CmdRecord, cmd_id: str, rt: ReceiverRuntime, ch: _Channel,
                              kind: str, coro) -> None:
        """Await the worker's CHANNEL_APPLIED and give the command its single result."""
        from ..devices.worker_client import WorkerFailed, WorkerGone, WorkerRejected, WorkerTimeout
        key = (s.principal.user, cmd_id)
        try:
            try:
                ap: wc.ChannelApplied | None = await coro if coro is not None else None
            except (WorkerRejected, WorkerFailed) as e:
                if kind == "close" and e.result is wc.Result.E_NOT_FOUND:
                    ap = None                         # already gone in the worker (e.g. restarted)
                else:
                    raise
            if kind == "close":
                self._reply_result(rec, cmd_id, "applied", channel=ch.id, channel_gen=ch.channel_gen)
                return
            current = self._channels.get(ch.id)
            if current is None or current.closing:
                # Closed (or its session ended) while the worker was applying: undo in the worker.
                if rt.link is not None and ap is not None and ap.open:
                    self._spawn(self._quiet(rt.link.channel_close(ch.worker_id, rt.cfg.command_timeout)))
                self._reply_result(rec, cmd_id, "failed", code=ErrorCode.E_UNKNOWN_CHANNEL,
                                   detail=f"{ch.id} was closed before the {kind} completed", channel=ch.id)
                return
            current.frequency_hz, current.mode = ch.frequency_hz, ch.mode
            current.agc, current.passband, current.manual_gain_db = ch.agc, ch.passband, ch.manual_gain_db
            current.applied_passband = self._applied_passband(ch.mode, ap)
            self._take_gen(current, ap.channel_gen)
            current.open = True
            self._publish_channel(current)
            self._reply_result(rec, cmd_id, "applied", channel=ch.id, channel_gen=current.channel_gen)
        except (WorkerRejected, WorkerFailed) as e:
            self._channel_failed(rec, cmd_id, ch, kind, WORKER_RESULT_TO_CODE.get(e.result, ErrorCode.E_INTERNAL),
                                 f"worker refused: {e.result.name}")
        except WorkerTimeout:
            self._channel_failed(rec, cmd_id, ch, kind, ErrorCode.E_TIMEOUT, "worker did not answer")
        except WorkerGone:
            self._channel_failed(rec, cmd_id, ch, kind, ErrorCode.E_DEVICE_FAILED, "receiver worker is not running")
        finally:
            self._release_channel_cmd(s.id)
            self._expire_cmds()

    def _channel_failed(self, rec: _CmdRecord, cmd_id: str, ch: _Channel, kind: str, code: ErrorCode,
                        detail: str) -> None:
        if kind == "open":
            existing = self._channels.get(ch.id)
            if existing is not None and not existing.open:
                self._channels.pop(ch.id, None)       # never applied: release its slot
        self._reply_result(rec, cmd_id, "failed", code=code, detail=detail, channel=ch.id)

    async def _reconcile_channels(self, rt: ReceiverRuntime, restarted: bool) -> None:
        """Keep every channel of a receiver on its absolute frequency after the capture changed.

        restarted: the worker is new and has no channels: open them again. Otherwise re-send each
        channel's config with the offset from the new centre (the worker rebuilt them on
        re-acquisition; this also yields the new channel_gen). A channel that no longer fits the
        capture is closed and removed from the state.
        """
        lock = self._reconcile_locks.setdefault(rt.id, asyncio.Lock())
        async with lock:
            from ..devices.worker_client import WorkerError
            for ch in [c for c in self._channels.values() if c.receiver == rt.id and c.open and not c.closing]:
                center, rate, complex_iq = self._capture_now(rt)
                problem = receiver_mode_problem(ch.mode, rate, complex_iq) or (
                    span_problem(ch.mode, ch.frequency_hz, center, rate, ch.passband)
                    if center is not None else "no centre")
                link = rt.link
                if problem or link is None:
                    self._drop_channel(ch, tell_worker=not restarted)
                    continue
                wch = self._worker_channel(ch, ch.frequency_hz - center)
                try:
                    ap = await (link.channel_open(wch, rt.cfg.command_timeout) if restarted
                                else link.channel_tune(wch, rt.cfg.command_timeout))
                except WorkerError:
                    self._drop_channel(ch, tell_worker=False)
                    continue
                if self._channels.get(ch.id) is ch and not ch.closing:
                    self._take_gen(ch, ap.channel_gen, new_worker=restarted)
                    ch.applied_passband = self._applied_passband(ch.mode, ap)
                    self._publish_channel(ch)

    def _spawn(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    # ----------------------------------------------------------------- jobs
    def start_stub_job(self, receiver: str, principal: str = "job:stub") -> str:
        """A server-owned job holding a receiver lease (placeholder for the M4 scanner)."""
        rt = self._receiver(receiver)
        job_id = f"job_stub-{next(self._job_ids)}"
        rt.lease.claim(Holder.for_job(principal, job_id), "deny")
        with self.store.txn() as t:
            t.add("jobs", job_id, value=m.JobState(kind="stub", receiver=receiver, state="running",
                                                   holds_lease=True, since_utc=datetime.now(timezone.utc)))
            t.replace("receivers", receiver, "lease", value=rt.lease.to_model())
        return job_id

    def stop_job(self, job_id: str) -> None:
        job = self.store.view()["jobs"].get(job_id)
        if job is None:
            raise KeyError(job_id)
        rt = self.receivers[job["receiver"]]
        holder = rt.lease.holder
        with self.store.txn() as t:
            if holder is not None and holder.job == job_id:
                rt.lease.release(holder)
                t.replace("receivers", job["receiver"], "lease", value=rt.lease.to_model())
            t.replace("jobs", job_id, value=dict(job, state="stopped", holds_lease=False,
                                                 since_utc=datetime.now(timezone.utc).isoformat()))

    def _on_preempt(self, receiver: str, prev: Holder, new: Holder) -> None:
        if prev.kind != "job" or prev.job is None:
            return
        job = self.store.view()["jobs"].get(prev.job)
        if job is None:
            return
        with self.store.txn() as t:
            t.replace("jobs", prev.job, value=dict(job, state="preempted", holds_lease=False,
                                                   detail=f"preempted by {new.principal}",
                                                   since_utc=datetime.now(timezone.utc).isoformat()))

    # ------------------------------------------------------------- decoders
    def submit_decoder_event(self, ev: DecoderEvent) -> bool:
        """Gate a decoder event; only current-generation events are kept."""
        if not self.gate.admit(ev):
            return False
        self.decoder_sink.append(ev)
        return True


_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")


def _stream_flags(hdr: FrameHeader) -> int:
    """frame_v1 flags -> stream v1 flags. Any recorded or overflow loss is a DISCONTINUITY."""
    out = sv.Flags(0)
    if FrameFlags.NEW_GEN in hdr.flags:
        out |= sv.Flags.NEW_GEN
    if hdr.flags & (FrameFlags.DISCONTINUITY | FrameFlags.OVERFLOW):
        out |= sv.Flags.DISCONTINUITY
    return int(out)


def _is_ulid(s: str) -> bool:
    return _ULID.fullmatch(s) is not None
