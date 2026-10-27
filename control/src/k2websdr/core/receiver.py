"""One receiver: its worker, its lease, its serialized command queue.

Rules implemented here (outline §4, §11, §13; acceptance A2, A3):
- Hardware commands for a receiver execute strictly one at a time, in
  submission order. The next command is not sent to the worker until the
  previous one has APPLIED, FAILED or timed out.
- A field's `applied` value changes only on the worker's APPLIED readback.
  Until then it shows `pending` with the old applied value.
- Failure keeps the last applied value and records the error code.
- A late APPLIED after a timeout updates the applied value (it is the truth
  about the device) even though the command was already reported failed.
- capture_gen is monotonic for the receiver's lifetime, including across
  worker restarts (the restarted worker starts at last_gen + 1).
- Worker crashes are restarted with bounded retries and backoff; after that
  the receiver stays `failed`, visibly.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..decoders.gate import GenerationGate
from ..protocol import models as m
from ..protocol import worker_ctl as wc
from ..protocol.errors import ErrorCode
from ..protocol.frame import FrameHeader
from ..replay.sigmf import Recording
from .lease import Lease
from .store import StateStore

log = logging.getLogger(__name__)

WORKER_RESULT_TO_CODE = {
    wc.Result.E_UNSUPPORTED: ErrorCode.E_UNSUPPORTED,
    wc.Result.E_OUT_OF_RANGE: ErrorCode.E_OUT_OF_RANGE,
    wc.Result.E_DEVICE: ErrorCode.E_DEVICE_FAILED,
    wc.Result.E_BUSY: ErrorCode.E_BUSY,
    wc.Result.E_BAD_MESSAGE: ErrorCode.E_INTERNAL,
    wc.Result.E_TIMEOUT: ErrorCode.E_TIMEOUT,
    wc.Result.E_NOT_FOUND: ErrorCode.E_UNKNOWN_CHANNEL,
}

_FORMAT_NAME = {1: "ci16", 2: "cf32", 3: "ri16", 4: "rf32"}
# Each capture field validates with its own constrained type (Hz / SampleRateHz).
_FIELD_TYPE = {"center_hz": m.FrequencyField, "sample_rate_hz": m.SampleRateField}


@dataclass
class ReceiverConfig:
    receiver_id: str
    label: str
    receiver_idx: int
    recording: Recording                      # M0: replay receivers only
    takeover_policy: m.TakeoverPolicy = "deny"
    command_timeout: float = 5.0
    max_restarts: int = 3
    restart_backoff: float = 0.5              # seconds, doubled per consecutive restart
    paced: bool = True
    loop: bool = True
    frame_samples: int = 4096


@dataclass
class CommandOutcome:
    status: str                               # "applied" | "failed"
    rev: int
    code: ErrorCode | None = None
    detail: str | None = None
    capture_gen: int | None = None


# Re-checked immediately before a queued command is sent to the worker.
# Returns None if the command may still run, else (error code, detail).
Authorizer = Callable[[], "tuple[ErrorCode, str] | None"]


@dataclass
class _Queued:
    cmd_id: str
    center_hz: int | None
    rate_hz: int | None
    future: asyncio.Future = field(repr=False)
    authorize: Authorizer | None = field(default=None, repr=False)


LinkFactory = Callable[["ReceiverRuntime"], "object"]   # returns a WorkerLink bound to this runtime


class ReceiverRuntime:
    def __init__(self, cfg: ReceiverConfig, store: StateStore, gate: GenerationGate,
                 link_factory: LinkFactory) -> None:
        self.cfg = cfg
        self.id = cfg.receiver_id
        self.store = store
        self.gate = gate
        self.lease = Lease(cfg.receiver_id, cfg.takeover_policy)
        self._link_factory = link_factory
        self._link = None
        self._queue: asyncio.Queue[_Queued] = asyncio.Queue()
        self._executor: asyncio.Task | None = None
        self._restart_task: asyncio.Task | None = None
        self._stopping = False
        self._restarts = 0
        self.capture_gen = 0
        self.frame_listeners: list[Callable[[str, FrameHeader, bytes], None]] = []
        self.executed: list[str] = []         # command ids in execution order (observability/tests)
        self._executing = False

    # ----------------------------------------------------------- state shape
    def initial_state(self) -> m.ReceiverState:
        rec = self.cfg.recording
        lo = hi = rec.freq_origin_hz
        return m.ReceiverState(
            label=self.cfg.label, driver="replay",
            acquisition=m.Acquisition(state="stopped"),
            lease=self.lease.to_model(),
            capture_gen=0,
            capture=m.Capture(
                center_hz=m.FrequencyField(),
                sample_rate_hz=m.SampleRateField(),
                antenna=m.FieldState[str](status="unsupported"),
            ),
            capabilities=m.Capabilities(
                freq_range_hz=None if not rec.format.is_complex else (lo, hi),
                sample_rates_hz=[rec.sample_rate_hz],
                antennas=[],
                sample_format=_FORMAT_NAME[int(rec.format)],
            ),
        )

    def _path(self, *parts):
        return ("receivers", self.id, *parts)

    def publish_lease(self) -> None:
        with self.store.txn() as t:
            t.replace(*self._path("lease"), value=self.lease.to_model())

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self._stopping = False
        if self._executor is None:
            self._executor = asyncio.create_task(self._execute(), name=f"rx-{self.id}-exec")
        await self._start_worker()

    async def _start_worker(self) -> None:
        from ..devices.worker_client import WorkerStartError

        gen_start = self.capture_gen + 1
        with self.store.txn() as t:
            t.replace(*self._path("acquisition"), value=m.Acquisition(state="starting"))
        self._link = self._link_factory(self)
        try:
            await self._link.start(gen_start)
        except WorkerStartError as e:
            detail = ("device locked by another process" if e.result is wc.Result.E_BUSY
                      else f"worker failed to start (exit {e.exit_code})")
            with self.store.txn() as t:
                t.replace(*self._path("acquisition"), value=m.Acquisition(state="failed", detail=detail))
            self._link = None
            raise
        rec = self.cfg.recording
        self.capture_gen = gen_start
        self.gate.set_capture_gen(self.id, gen_start)
        with self.store.txn() as t:
            t.replace(*self._path("acquisition"), value=m.Acquisition(state="running"))
            t.replace(*self._path("capture_gen"), value=gen_start)
            # Replay parameters are facts from the recording, not a device readback.
            t.replace(*self._path("capture", "center_hz"),
                      value=m.FrequencyField(requested=rec.freq_origin_hz, applied=rec.freq_origin_hz,
                                              status="applied", verified="none"))
            t.replace(*self._path("capture", "sample_rate_hz"),
                      value=m.SampleRateField(requested=rec.sample_rate_hz, applied=rec.sample_rate_hz,
                                              status="applied", verified="none"))

    async def stop(self) -> None:
        self._stopping = True
        if self._restart_task:
            self._restart_task.cancel()
        if self._link is not None:
            await self._link.stop(self.cfg.command_timeout)
        if self._executor:
            self._executor.cancel()
            await asyncio.gather(self._executor, return_exceptions=True)
            self._executor = None
        with self.store.txn() as t:
            t.replace(*self._path("acquisition"), value=m.Acquisition(state="stopped"))

    # ------------------------------------------------------ worker callbacks
    def on_frame(self, hdr: FrameHeader, payload: bytes) -> None:
        for fn in list(self.frame_listeners):
            fn(self.id, hdr, payload)

    def on_status(self, st: wc.Status) -> None:
        if st.state is wc.WorkerState.FAILED:
            with self.store.txn() as t:
                t.replace(*self._path("acquisition"),
                          value=m.Acquisition(state="failed", detail=f"worker reported {st.result.name}"))

    def on_late_applied(self, ap: wc.Applied) -> None:
        self._record_applied(ap, cmd_id=None)

    def on_exit(self, code: int) -> None:
        if self._stopping:
            return
        self._link = None
        with self.store.txn() as t:
            t.replace(*self._path("acquisition"),
                      value=m.Acquisition(state="failed", detail=f"worker exited with code {code}"))
        if self._restarts < self.cfg.max_restarts:
            delay = self.cfg.restart_backoff * (2 ** self._restarts)
            self._restarts += 1
            self._restart_task = asyncio.get_running_loop().create_task(self._restart_after(delay))

    async def _restart_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if self._stopping:
            return
        try:
            await self._start_worker()
        except Exception as e:  # noqa: BLE001 - state already shows failed; on_exit decides on retries
            log.warning("%s: restart failed: %s", self.id, e)

    @property
    def outstanding(self) -> int:
        """Hardware commands queued or executing on this receiver."""
        return self._queue.qsize() + (1 if self._executing else 0)

    @property
    def restarts(self) -> int:
        return self._restarts

    @property
    def link(self):
        return self._link

    # ------------------------------------------------------------- commands
    def submit_set_capture(self, cmd_id: str, center_hz: int | None, rate_hz: int | None,
                           authorize: Authorizer | None = None) -> tuple[int, asyncio.Future]:
        """Mark the requested fields pending (one revision) and queue the command.

        `authorize` is re-run immediately before the command is sent to the
        worker: ownership checked at submission can have changed while the
        command waited behind others (lease taken over, session ended,
        generation moved on). A command that fails it never reaches hardware
        and still gets exactly one (failed) outcome.
        """
        with self.store.txn() as t:
            for name, value in (("center_hz", center_hz), ("sample_rate_hz", rate_hz)):
                if value is None:
                    continue
                cur = t.get(*self._path("capture", name))
                t.replace(*self._path("capture", name), value=_FIELD_TYPE[name](
                    requested=value, applied=cur["applied"], status="pending", cmd=cmd_id,
                    verified=cur["verified"] if cur["applied"] is not None else "none"))
        fut = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(_Queued(cmd_id, center_hz, rate_hz, fut, authorize))
        return self.store.rev, fut

    async def _execute(self) -> None:
        while True:
            q = await self._queue.get()
            self._executing = True
            try:
                await self._run_one(q)
            finally:
                self._executing = False

    async def _run_one(self, q: _Queued) -> None:
        """Re-authorize, execute and resolve one command. Always resolves q.future exactly once."""
        from ..devices.worker_client import WorkerGone, WorkerRejected, WorkerFailed, WorkerTimeout

        denied = q.authorize() if q.authorize is not None else None
        if denied is not None:
            outcome = self._record_failure(q, denied[0], detail=denied[1])
        else:
            self.executed.append(q.cmd_id)
            try:
                if self._link is None:
                    raise WorkerGone("receiver is not running")
                ap = await self._link.set_capture(q.center_hz, q.rate_hz, self.cfg.command_timeout)
            except (WorkerRejected, WorkerFailed) as e:
                outcome = self._record_failure(q, WORKER_RESULT_TO_CODE.get(e.result, ErrorCode.E_INTERNAL))
            except WorkerTimeout:
                outcome = self._record_failure(q, ErrorCode.E_TIMEOUT)
            except WorkerGone:
                outcome = self._record_failure(q, ErrorCode.E_DEVICE_FAILED)
            else:
                rev = self._record_applied(ap, cmd_id=q.cmd_id, fields=self._fields(q))
                outcome = CommandOutcome("applied", rev, capture_gen=ap.capture_gen)
        if not q.future.done():
            q.future.set_result(outcome)

    @staticmethod
    def _fields(q: _Queued) -> list[str]:
        return [n for n, v in (("center_hz", q.center_hz), ("sample_rate_hz", q.rate_hz)) if v is not None]

    def _record_applied(self, ap: wc.Applied, cmd_id: str | None, fields: list[str] | None = None) -> int:
        """Apply a worker readback. Three cases per field:

        1. The field is owned by this command: applied := readback, status applied.
        2. The field is pending under another (later) command: keep it pending,
           but its applied value still tracks the device.
        3. Anything else (including a late APPLIED after a timeout): applied :=
           readback; status and error are left as they are, because the command
           outcome already reported stays true.
        """
        if ap.capture_gen < self.capture_gen:
            raise RuntimeError(f"{self.id}: worker reported capture_gen {ap.capture_gen} < {self.capture_gen}")
        self.capture_gen = ap.capture_gen
        self.gate.set_capture_gen(self.id, ap.capture_gen)
        verified = "driver_readback" if ap.verified == 1 else "none"
        readback = {"center_hz": ap.center_hz, "sample_rate_hz": ap.sample_rate_hz}
        with self.store.txn() as t:
            t.replace(*self._path("capture_gen"), value=ap.capture_gen)
            for name, value in readback.items():
                cur = t.get(*self._path("capture", name))
                owned = cmd_id is not None and name in (fields or []) and cur["cmd"] == cmd_id
                if owned:
                    new = dict(cur, applied=value, status="applied", verified=verified, error=None)
                elif cur["applied"] != value or cur["verified"] != verified:
                    new = dict(cur, applied=value, verified=verified)
                else:
                    continue
                t.replace(*self._path("capture", name), value=new)   # validated with the whole state at commit
        return self.store.rev

    def _record_failure(self, q: _Queued, code: ErrorCode, detail: str | None = None) -> CommandOutcome:
        with self.store.txn() as t:
            for name in self._fields(q):
                cur = t.get(*self._path("capture", name))
                if cur["cmd"] != q.cmd_id:
                    continue        # a later command already owns this field
                t.replace(*self._path("capture", name), value=_FIELD_TYPE[name](
                    requested=cur["requested"], applied=cur["applied"], status="failed", cmd=q.cmd_id,
                    verified=cur["verified"] if cur["applied"] is not None else "none", error=code))
        return CommandOutcome("failed", self.store.rev, code=code, detail=detail)


def replay_link_factory(worker_bin: Path, lock_dir: Path) -> LinkFactory:
    """Real worker processes for replay receivers."""
    from ..devices.worker_client import Callbacks, WorkerProcess
    from ..replay.worker_argv import WorkerIdentity, replay_argv

    def factory(rt: ReceiverRuntime):
        ident = WorkerIdentity(device_id=rt.id, lock_dir=lock_dir, receiver_idx=rt.cfg.receiver_idx)

        def argv(ctl_fd: int, data_fd: int, gen_start: int) -> list[str]:
            return replay_argv(worker_bin, rt.cfg.recording, ident, ctl_fd=ctl_fd, data_fd=data_fd,
                               frame_samples=rt.cfg.frame_samples, paced=rt.cfg.paced, loop=rt.cfg.loop,
                               capture_gen_start=gen_start)

        return WorkerProcess(argv, Callbacks(on_frame=rt.on_frame, on_status=rt.on_status,
                                             on_late_applied=rt.on_late_applied, on_exit=rt.on_exit))

    return factory
