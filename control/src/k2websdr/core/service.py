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
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from ..decoders.gate import GenerationGate
from ..protocol import models as m
from ..protocol.decoder import DecoderEvent
from ..protocol.errors import ErrorCode
from .lease import Holder, LeaseError
from .receiver import ReceiverConfig, ReceiverRuntime
from .store import StateStore

IDEMPOTENCY_TTL = 600.0
# Command capacity (see docs/protocol/control-v1.md, "Capacity"):
MAX_CMD_RECORDS = 10_000      # total idempotency records: in-flight + finished
MAX_IN_FLIGHT = 1_000         # accepted hardware commands not yet finished, all receivers
RECEIVER_QUEUE_LIMIT = 64     # per receiver: executing + queued hardware commands


@dataclass(frozen=True)
class Principal:
    user: str                                   # e.g. "user:adam"
    permissions: frozenset[str]


class Session:
    _ids = itertools.count(1)

    def __init__(self, principal: Principal) -> None:
        self.id = f"s_{next(self._ids):06d}{int(time.time() * 1000) % 100000:05d}"
        self.principal = principal
        self.outbox: asyncio.Queue[m.BaseModel] = asyncio.Queue()
        self.subscribed = False
        self.closed = False

    @property
    def holder(self) -> Holder:
        return Holder.operator(self.principal.user, self.id)

    def send(self, msg: m.BaseModel) -> None:
        if not self.closed:
            m.dump_server_message(msg)          # validates before anything leaves the server
            self.outbox.put_nowait(msg)


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
                 in_flight_capacity: int | None = None, receiver_queue_limit: int | None = None) -> None:
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
        self.decoder_sink: list[DecoderEvent] = []
        self.store.subscribe(self._broadcast)

    # ------------------------------------------------------------ receivers
    def add_receiver(self, cfg: ReceiverConfig, link_factory: Callable[[ReceiverRuntime], Any]) -> ReceiverRuntime:
        if cfg.receiver_id in self.receivers:
            raise ValueError(f"duplicate receiver {cfg.receiver_id}")
        rt = ReceiverRuntime(cfg, self.store, self.gate, link_factory)
        rt.lease.on_preempt(lambda prev, new, rid=cfg.receiver_id: self._on_preempt(rid, prev, new))
        with self.store.txn() as t:
            t.add("receivers", cfg.receiver_id, value=rt.initial_state())
        self.receivers[cfg.receiver_id] = rt
        return rt

    async def start(self) -> None:
        for rt in self.receivers.values():
            await rt.start()

    async def shutdown(self) -> None:
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
        for rt in self.receivers.values():
            if rt.lease.release_session(s.id):
                rt.publish_lease()

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
        try:
            needed = m.OP_PERMISSION[cmd.op]
            if needed is not None and needed not in s.principal.permissions:
                raise Rejected(ErrorCode.E_FORBIDDEN, f"{cmd.op} requires permission {needed}")
            await getattr(self, "_op_" + cmd.op.replace(".", "_"))(s, cmd)
        except CapacityRejected as r:
            s.send(m.Ack(v=1, type="ack", id=cmd.id, status="rejected", code=r.code, detail=r.detail))
        except Rejected as r:
            self._reply_ack(s, cmd.id, "rejected", code=r.code, detail=r.detail)
        except LeaseError as le:
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
                      detail: str | None = None, capture_gen: int | None = None) -> None:
        rec.result = m.Result(v=1, type="result", id=cmd_id, status=status, code=code, detail=detail,
                              rev=self.store.rev, capture_gen=capture_gen)
        rec.finished = time.monotonic()
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
        self._check_capacity(rt)                  # before anything is marked pending or queued
        _rev, fut = rt.submit_set_capture(cmd.id, a.center_hz, a.sample_rate_hz,
                                          authorize=self._authorizer(s, rt, "hw.retune", a.if_capture_gen))
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

    async def _op_channel_open(self, s: Session, cmd: m.ChannelOpen) -> None:
        self._receiver(cmd.args.receiver)
        if cmd.args.mode not in self.store.view()["ledger"]["available"]:
            raise Rejected(ErrorCode.E_MODE_UNAVAILABLE, f"mode {cmd.args.mode} is not available")
        raise Rejected(ErrorCode.E_UNSUPPORTED, "channels are not implemented before M1")

    async def _op_channel_tune(self, s: Session, cmd: m.ChannelTune) -> None:
        raise Rejected(ErrorCode.E_UNKNOWN_CHANNEL, f"no channel {cmd.args.channel}")

    async def _op_channel_close(self, s: Session, cmd: m.ChannelClose) -> None:
        raise Rejected(ErrorCode.E_UNKNOWN_CHANNEL, f"no channel {cmd.args.channel}")

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


def _is_ulid(s: str) -> bool:
    return _ULID.fullmatch(s) is not None
