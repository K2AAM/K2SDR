"""A scriptable stand-in for WorkerProcess, for lifecycle tests.

Each set_capture call consumes the next scripted behaviour:
  ("apply",)                  readback = requested values (unchanged fields keep current)
  ("apply", center, rate)     explicit readback (e.g. hardware rounding)
  ("reject", Result)          worker ACK with an error
  ("fail", Result)            worker ACK OK, then FAILED
  ("hang",)                   never answers (caller times out)
  ("crash", exit_code)        worker dies mid-command
An optional gate (asyncio.Event) holds every reply until released, so tests
can observe the pending state.
"""

from __future__ import annotations

import asyncio
from collections import deque

from k2websdr.devices.worker_client import (WorkerGone, WorkerRejected, WorkerFailed, WorkerStartError,
                                            WorkerTimeout)
from k2websdr.protocol import worker_ctl as wc


class FakeWorker:
    instances: list["FakeWorker"] = []

    def __init__(self, runtime, script: deque, *, gate: asyncio.Event | None = None,
                 start_result: wc.Result | None = None) -> None:
        self.rt = runtime
        self.script = script
        self.gate = gate
        self.start_result = start_result
        self.calls: list[tuple[int | None, int | None]] = []
        self.active = 0
        self.max_active = 0
        self.gen = 0
        self.center = runtime.cfg.recording.freq_origin_hz
        self.rate = runtime.cfg.recording.sample_rate_hz
        self.alive = False
        FakeWorker.instances.append(self)

    async def start(self, capture_gen_start: int) -> None:
        if self.start_result is not None:
            raise WorkerStartError(self.start_result, 75, "fake start failure")
        self.gen = capture_gen_start
        self.alive = True

    async def set_capture(self, center_hz, rate_hz, timeout):
        if not self.alive:
            raise WorkerGone("not running")
        self.calls.append((center_hz, rate_hz))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            step = self.script.popleft() if self.script else ("apply",)
            if self.gate is not None:
                await self.gate.wait()
            kind = step[0]
            if kind == "hang":
                await asyncio.sleep(timeout)
                raise WorkerTimeout("fake hang")
            if kind == "reject":
                raise WorkerRejected(step[1])
            if kind == "fail":
                raise WorkerFailed(step[1])
            if kind == "crash":
                self.alive = False
                self.rt.on_exit(step[1])
                raise WorkerGone("fake crash")
            center = step[1] if len(step) > 1 else (center_hz if center_hz is not None else self.center)
            rate = step[2] if len(step) > 2 else (rate_hz if rate_hz is not None else self.rate)
            self.gen += 1
            self.center, self.rate = center, rate
            return wc.Applied(capture_gen=self.gen, center_hz=center, sample_rate_hz=rate, verified=1)
        finally:
            self.active -= 1

    def late_apply(self, center: int, rate: int) -> None:
        self.gen += 1
        self.rt.on_late_applied(wc.Applied(self.gen, center, rate, 1))

    async def stop(self, timeout: float) -> None:
        self.alive = False

    def kill(self) -> None:
        self.alive = False


def fake_factory(script: deque | None = None, **kw):
    def factory(rt):
        return FakeWorker(rt, script if script is not None else deque(), **kw)
    return factory
