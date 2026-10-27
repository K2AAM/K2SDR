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
  the receiver stays `failed`, visibly. Live receivers (max_restarts=None) retry without limit,
  backoff capped, so an Airspy unplugged and plugged back in (or released by another program)
  comes back by itself; the retry is visible in `acquisition`.
- Live receivers (M1, Airspy HF+): the hardware gain is serialized with retunes in the same
  queue; a restarted worker opens with the last applied centre, rate and gain (the device keeps
  whatever was set last, so the worker sets every control on open).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
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


# Sample rates the Airspy HF+ Discovery reported in Window 1 (firmware R5.0.1-CD, lib 1.6.8).
AIRSPYHF_MEASURED_RATES_HZ = (912000, 768000, 650000, 456000, 384000, 228000, 192000)


@dataclass(frozen=True)
class AirspyhfSpec:
    """A live Airspy HF+ receiver (M1). Ranges and rates are the admin's safe values (outline:
    devices "set safe ranges and rates"); the worker re-checks against the device."""
    serial: int
    center_hz: int                            # initial centre
    rate_hz: int                              # initial rate
    freq_range_hz: tuple[int, int]            # safe tuning range for the centre
    sample_rates_hz: tuple[int, ...] = AIRSPYHF_MEASURED_RATES_HZ
    gain: m.HwGain = m.HwGain(hf_agc=True, hf_agc_threshold="low", hf_lna=True, hf_att=0)   # approved defaults

    def __post_init__(self) -> None:
        lo, hi = self.freq_range_hz
        if not 0 < lo < hi <= 0xFFFFFFFF:
            raise ValueError("freq_range_hz must be 0 < lo < hi <= 4294967295")
        if not lo <= self.center_hz <= hi:
            raise ValueError("center_hz outside freq_range_hz")
        if not self.sample_rates_hz or set(self.sample_rates_hz) - set(AIRSPYHF_MEASURED_RATES_HZ):
            raise ValueError(f"sample_rates_hz must be a subset of {AIRSPYHF_MEASURED_RATES_HZ}")
        if self.rate_hz not in self.sample_rates_hz:
            raise ValueError("rate_hz not in sample_rates_hz")
        if not 0 < self.serial < 1 << 64:
            raise ValueError("serial must be a non-zero u64")


@dataclass
class ReceiverConfig:
    receiver_id: str
    label: str
    receiver_idx: int
    recording: Recording | None = None        # replay receivers
    airspyhf: AirspyhfSpec | None = None      # live Airspy HF+ receivers (M1); exactly one of the two
    takeover_policy: m.TakeoverPolicy = "deny"
    command_timeout: float = 5.0
    max_restarts: int | None = 3              # None: retry without limit (live receivers)
    restart_backoff: float = 0.5              # seconds, doubled per consecutive restart
    restart_backoff_max: float = 30.0
    healthy_after_s: float = 60.0             # a worker that ran this long resets the restart count
    paced: bool = True
    loop: bool = True
    frame_samples: int = 4096
    iq_frames: bool = True                    # raw IQ frames to the service (decoders); off for listening only
    spectrum_fft: int = 0                     # 0: no SPECTRUM frames; else a power of two 64..65536
    spectrum_fps: int = 15


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
    gain: m.HwGain | None = None              # set: a SET_HW command (the full desired gain)


LinkFactory = Callable[["ReceiverRuntime"], "object"]   # returns a WorkerLink bound to this runtime


class ReceiverRuntime:
    def __init__(self, cfg: ReceiverConfig, store: StateStore, gate: GenerationGate,
                 link_factory: LinkFactory) -> None:
        if (cfg.recording is None) == (cfg.airspyhf is None):
            raise ValueError(f"{cfg.receiver_id}: exactly one of recording / airspyhf")
        self.cfg = cfg
        self.id = cfg.receiver_id
        sp = cfg.airspyhf
        # Live receivers: what a (re)started worker opens with; follows every applied readback.
        self.live_center_hz: int | None = sp.center_hz if sp else None
        self.live_rate_hz: int | None = sp.rate_hz if sp else None
        self.live_gain: m.HwGain | None = sp.gain if sp else None
        self._started_at: float | None = None
        self._retry_in: float | None = None
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
        # Called after the capture may have changed under the listener channels: a new readback
        # (restarted=False) or a fresh worker that has no channels yet (restarted=True).
        self.on_capture_change: Callable[["ReceiverRuntime", bool], None] | None = None
        self.executed: list[str] = []         # command ids in execution order (observability/tests)
        self._executing = False

    # ----------------------------------------------------------- state shape
    def initial_state(self) -> m.ReceiverState:
        sp = self.cfg.airspyhf
        if sp is not None:
            return m.ReceiverState(
                label=self.cfg.label, driver="airspyhf",
                acquisition=m.Acquisition(state="stopped"),
                lease=self.lease.to_model(),
                capture_gen=0,
                capture=m.Capture(
                    center_hz=m.FrequencyField(),
                    sample_rate_hz=m.SampleRateField(),
                    antenna=m.FieldState[str](status="unsupported"),
                    hw_gain=m.HwGainField(),
                ),
                capabilities=m.Capabilities(freq_range_hz=sp.freq_range_hz, sample_rates_hz=list(sp.sample_rates_hz),
                                            antennas=[], sample_format="cf32"),
            )
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
            self._link = None
            state, detail = self._classify(e.result, e.exit_code)
            self._publish_acquisition(state, detail)
            raise
        self.capture_gen = gen_start
        self._started_at = time.monotonic()
        self.gate.set_capture_gen(self.id, gen_start)
        with self.store.txn() as t:
            t.replace(*self._path("acquisition"), value=m.Acquisition(state="running"))
            t.replace(*self._path("capture_gen"), value=gen_start)
            if self.cfg.airspyhf is not None:
                # What the worker set on open (no getters: "applied" means set and accepted).
                for name, value in (("center_hz", self.live_center_hz), ("sample_rate_hz", self.live_rate_hz)):
                    cur = t.get(*self._path("capture", name))
                    t.replace(*self._path("capture", name), value=_FIELD_TYPE[name](
                        requested=cur["requested"] if cur["status"] == "pending" else value, applied=value,
                        status=cur["status"] if cur["status"] == "pending" else "applied",
                        cmd=cur["cmd"], verified="none"))
                cur = t.get(*self._path("capture", "hw_gain"))
                t.replace(*self._path("capture", "hw_gain"), value=m.HwGainField(
                    requested=cur["requested"] if cur["status"] == "pending" else self.live_gain,
                    applied=self.live_gain, status=cur["status"] if cur["status"] == "pending" else "applied",
                    cmd=cur["cmd"], verified="none"))
            else:
                rec = self.cfg.recording
                # Replay parameters are facts from the recording, not a device readback.
                t.replace(*self._path("capture", "center_hz"),
                          value=m.FrequencyField(requested=rec.freq_origin_hz, applied=rec.freq_origin_hz,
                                                  status="applied", verified="none"))
                t.replace(*self._path("capture", "sample_rate_hz"),
                          value=m.SampleRateField(requested=rec.sample_rate_hz, applied=rec.sample_rate_hz,
                                                  status="applied", verified="none"))
        if self.on_capture_change is not None:
            self.on_capture_change(self, True)

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

    def on_late_hw_applied(self, ap: wc.HwApplied) -> None:
        self._record_hw_applied(ap, cmd_id=None)

    # Worker exit codes (worker/src/worker.hpp) and start results -> acquisition state.
    @staticmethod
    def _classify(result: wc.Result | None, code: int | None) -> tuple[str, str]:
        if result is wc.Result.E_BUSY or code == 75:
            return "external_busy", "device in use by another process"
        if code == 69 or result is wc.Result.E_DEVICE:
            return "failed", "device not found or lost"
        if code == 71:
            return "failed", "device did not stop cleanly"
        return "failed", f"worker exited with code {code}"

    def _publish_acquisition(self, state: str, detail: str) -> None:
        if self._retry_in is not None:
            detail = f"{detail}; retrying in {self._retry_in:g} s"
        with self.store.txn() as t:
            t.replace(*self._path("acquisition"), value=m.Acquisition(state=state, detail=detail[:200]))

    def on_exit(self, code: int) -> None:
        if self._stopping:
            return
        self._link = None
        if self._started_at is not None and time.monotonic() - self._started_at >= self.cfg.healthy_after_s:
            self._restarts = 0                    # it had been working: a new incident, not a crash loop
        self._started_at = None
        self._retry_in = None
        limit = self.cfg.max_restarts
        if limit is None or self._restarts < limit:
            delay = min(self.cfg.restart_backoff * (2 ** self._restarts), self.cfg.restart_backoff_max)
            self._restarts += 1
            self._retry_in = delay
            self._restart_task = asyncio.get_running_loop().create_task(self._restart_after(delay))
        self._publish_acquisition(*self._classify(None, code))

    async def _restart_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if self._stopping:
            return
        self._retry_in = None
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

    def submit_set_hw_gain(self, cmd_id: str, gain: m.HwGain,
                           authorize: Authorizer | None = None) -> tuple[int, asyncio.Future]:
        """Mark hw_gain pending with the full desired gain and queue it behind any retune."""
        with self.store.txn() as t:
            cur = t.get(*self._path("capture", "hw_gain"))
            t.replace(*self._path("capture", "hw_gain"), value=m.HwGainField(
                requested=gain, applied=cur["applied"], status="pending", cmd=cmd_id,
                verified=cur["verified"] if cur["applied"] is not None else "none"))
        fut = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(_Queued(cmd_id, None, None, fut, authorize, gain=gain))
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
                if q.gain is not None:
                    hw = await self._link.set_hw(_set_hw(q.gain), self.cfg.command_timeout)
                else:
                    ap = await self._link.set_capture(q.center_hz, q.rate_hz, self.cfg.command_timeout)
            except (WorkerRejected, WorkerFailed) as e:
                outcome = self._record_failure(q, WORKER_RESULT_TO_CODE.get(e.result, ErrorCode.E_INTERNAL))
            except WorkerTimeout:
                outcome = self._record_failure(q, ErrorCode.E_TIMEOUT)
            except WorkerGone:
                outcome = self._record_failure(q, ErrorCode.E_DEVICE_FAILED)
            else:
                if q.gain is not None:
                    rev = self._record_hw_applied(hw, cmd_id=q.cmd_id)
                    outcome = CommandOutcome("applied", rev, capture_gen=hw.capture_gen)
                else:
                    rev = self._record_applied(ap, cmd_id=q.cmd_id, fields=self._fields(q))
                    outcome = CommandOutcome("applied", rev, capture_gen=ap.capture_gen)
        if not q.future.done():
            q.future.set_result(outcome)

    @staticmethod
    def _fields(q: _Queued) -> list[str]:
        if q.gain is not None:
            return ["hw_gain"]
        return [n for n, v in (("center_hz", q.center_hz), ("sample_rate_hz", q.rate_hz)) if v is not None]

    def _record_hw_applied(self, ap: wc.HwApplied, cmd_id: str | None) -> int:
        """HW_APPLIED: what the worker set. Same ownership rules as the capture fields."""
        gain = m.HwGain(hf_agc=ap.hf_agc, hf_agc_threshold="high" if ap.hf_agc_threshold_high else "low",
                        hf_lna=ap.hf_lna, hf_att=ap.hf_att)
        self.live_gain = gain
        with self.store.txn() as t:
            cur = t.get(*self._path("capture", "hw_gain"))
            applied = json.loads(gain.model_dump_json())
            if cmd_id is not None and cur["cmd"] == cmd_id:
                new = dict(cur, applied=applied, status="applied", verified="none", error=None)
            elif cur["applied"] != applied:
                new = dict(cur, applied=applied)
            else:
                return self.store.rev
            t.replace(*self._path("capture", "hw_gain"), value=new)
        return self.store.rev

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
        if self.cfg.airspyhf is not None:          # a restarted worker opens where the device was
            self.live_center_hz, self.live_rate_hz = ap.center_hz, ap.sample_rate_hz
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
        if self.on_capture_change is not None:
            self.on_capture_change(self, False)
        return self.store.rev

    def _record_failure(self, q: _Queued, code: ErrorCode, detail: str | None = None) -> CommandOutcome:
        with self.store.txn() as t:
            for name in self._fields(q):
                cur = t.get(*self._path("capture", name))
                if cur["cmd"] != q.cmd_id:
                    continue        # a later command already owns this field
                if name == "hw_gain":
                    t.replace(*self._path("capture", name), value=dict(cur, status="failed", error=code))
                    continue
                t.replace(*self._path("capture", name), value=_FIELD_TYPE[name](
                    requested=cur["requested"], applied=cur["applied"], status="failed", cmd=q.cmd_id,
                    verified=cur["verified"] if cur["applied"] is not None else "none", error=code))
        return CommandOutcome("failed", self.store.rev, code=code, detail=detail)


def _set_hw(g: m.HwGain) -> wc.SetHw:
    """Every control, every time (the worker sets all four anyway)."""
    return wc.SetHw(hf_agc=g.hf_agc, hf_agc_threshold_high=g.hf_agc_threshold == "high", hf_lna=g.hf_lna,
                    hf_att=g.hf_att)


def airspyhf_device_id(serial: int) -> str:
    """Lock name: one physical Airspy HF+ per serial, whichever receiver config names it."""
    return f"airspyhf-{serial:016x}"


def airspyhf_link_factory(worker_bin: Path, lock_dir: Path) -> LinkFactory:
    """Real worker processes for live Airspy HF+ receivers (M1). Each (re)start opens with the
    receiver's current centre, rate and gain, not the configured initial ones."""
    from ..devices.worker_client import Callbacks, WorkerProcess
    from ..replay.worker_argv import HwGain, WorkerIdentity, airspyhf_argv

    def factory(rt: ReceiverRuntime):
        sp = rt.cfg.airspyhf
        ident = WorkerIdentity(device_id=airspyhf_device_id(sp.serial), lock_dir=lock_dir,
                               receiver_idx=rt.cfg.receiver_idx)

        def argv(ctl_fd: int, data_fd: int, gen_start: int) -> list[str]:
            g = rt.live_gain
            return airspyhf_argv(worker_bin, ident, serial=sp.serial, rate_hz=rt.live_rate_hz,
                                 center_hz=rt.live_center_hz, ctl_fd=ctl_fd, data_fd=data_fd,
                                 gain=HwGain(g.hf_agc, g.hf_agc_threshold == "high", g.hf_lna, g.hf_att),
                                 frame_samples=rt.cfg.frame_samples, capture_gen_start=gen_start,
                                 iq_frames=rt.cfg.iq_frames, spectrum_fft=rt.cfg.spectrum_fft,
                                 spectrum_fps=rt.cfg.spectrum_fps)

        return WorkerProcess(argv, Callbacks(on_frame=rt.on_frame, on_status=rt.on_status,
                                             on_late_applied=rt.on_late_applied,
                                             on_late_hw_applied=rt.on_late_hw_applied, on_exit=rt.on_exit))

    return factory


def replay_link_factory(worker_bin: Path, lock_dir: Path) -> LinkFactory:
    """Real worker processes for replay receivers."""
    from ..devices.worker_client import Callbacks, WorkerProcess
    from ..replay.worker_argv import WorkerIdentity, replay_argv

    def factory(rt: ReceiverRuntime):
        ident = WorkerIdentity(device_id=rt.id, lock_dir=lock_dir, receiver_idx=rt.cfg.receiver_idx)

        def argv(ctl_fd: int, data_fd: int, gen_start: int) -> list[str]:
            return replay_argv(worker_bin, rt.cfg.recording, ident, ctl_fd=ctl_fd, data_fd=data_fd,
                               frame_samples=rt.cfg.frame_samples, paced=rt.cfg.paced, loop=rt.cfg.loop,
                               capture_gen_start=gen_start, iq_frames=rt.cfg.iq_frames,
                               spectrum_fft=rt.cfg.spectrum_fft, spectrum_fps=rt.cfg.spectrum_fps)

        return WorkerProcess(argv, Callbacks(on_frame=rt.on_frame, on_status=rt.on_status,
                                             on_late_applied=rt.on_late_applied, on_exit=rt.on_exit))

    return factory
