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

Listener channels follow the real worker's semantics (docs/protocol/worker-dsp-v1.md):
offset-only CHANNEL_TUNE keeps channel_gen, any other change and every re-acquisition bump it;
unknown channels give E_NOT_FOUND, duplicates E_BUSY. `channel_script` scripts failures
(("reject", Result) / ("hang",)); `channel_gate` holds channel replies until released.
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
                 start_result: wc.Result | None = None, channel_script: deque | None = None,
                 channel_gate: asyncio.Event | None = None) -> None:
        self.channels: dict[int, wc.Channel] = {}
        self.channel_gens: dict[int, int] = {}
        self.next_channel_gen = 1
        self.channel_calls: list[tuple[str, int, int | None]] = []   # (op, channel_id, offset)
        self.channel_script = channel_script if channel_script is not None else deque()
        self.channel_gate = channel_gate
        self.rt = runtime
        self.script = script
        self.gate = gate
        self.start_result = start_result
        self.calls: list[tuple[int | None, int | None]] = []
        self.active = 0
        self.max_active = 0
        self.gen = 0
        if runtime.cfg.airspyhf is not None:          # opens where the runtime says (restart: last applied)
            self.center, self.rate = runtime.live_center_hz, runtime.live_rate_hz
            g = runtime.live_gain
            self.opened_with = (self.center, self.rate, g)
            self.hw = wc.HwApplied(0, g.hf_agc, g.hf_agc_threshold == "high", g.hf_lna, g.hf_att, 0)
        else:
            self.center = runtime.cfg.recording.freq_origin_hz
            self.rate = runtime.cfg.recording.sample_rate_hz
        self.hw_calls: list[wc.SetHw] = []
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
            for cid in self.channels:                       # re-acquisition rebuilds every channel
                self.channel_gens[cid] = self._bump()
            return wc.Applied(capture_gen=self.gen, center_hz=center, sample_rate_hz=rate, verified=1)
        finally:
            self.active -= 1

    async def set_hw(self, hw: wc.SetHw, timeout: float = 5.0) -> wc.HwApplied:
        if not self.alive:
            raise WorkerGone("not running")
        if self.rt.cfg.airspyhf is None:
            raise WorkerRejected(wc.Result.E_UNSUPPORTED)
        self.hw_calls.append(hw)
        self.calls.append(("hw", None))
        step = self.script.popleft() if self.script else ("apply",)
        if self.gate is not None:
            await self.gate.wait()
        if step[0] == "fail":
            raise WorkerFailed(step[1])
        if step[0] == "hang":
            await asyncio.sleep(timeout)
            raise WorkerTimeout("fake hang")
        if step[0] == "crash":
            self.alive = False
            self.rt.on_exit(step[1])
            raise WorkerGone("fake crash")
        h = self.hw
        self.hw = wc.HwApplied(self.gen, *(old if new is None else new for old, new in (
            (h.hf_agc, hw.hf_agc), (h.hf_agc_threshold_high, hw.hf_agc_threshold_high), (h.hf_lna, hw.hf_lna),
            (h.hf_att, hw.hf_att))), 0)
        return self.hw

    # ---------------------------------------------------------------- channels
    def _bump(self) -> int:
        g = self.next_channel_gen
        self.next_channel_gen += 1
        return g

    async def _channel_step(self, timeout):
        if not self.alive:
            raise WorkerGone("not running")
        if self.channel_gate is not None:
            await self.channel_gate.wait()
        step = self.channel_script.popleft() if self.channel_script else ("apply",)
        if step[0] == "reject":
            raise WorkerRejected(step[1])
        if step[0] == "hang":
            await asyncio.sleep(timeout)
            raise WorkerTimeout("fake hang")

    def _applied(self, cid: int, open_: bool = True) -> wc.ChannelApplied:
        return wc.ChannelApplied(self.channel_gens[cid], self.gen, self.channels[cid], 12000, open_)

    async def channel_open(self, ch: wc.Channel, timeout: float = 5.0) -> wc.ChannelApplied:
        self.channel_calls.append(("open", ch.channel_id, ch.offset_hz))
        await self._channel_step(timeout)
        if ch.channel_id in self.channels:
            raise WorkerRejected(wc.Result.E_BUSY)
        if abs(ch.offset_hz) > self.rate // 2:
            raise WorkerRejected(wc.Result.E_OUT_OF_RANGE)
        self.channels[ch.channel_id] = ch
        self.channel_gens[ch.channel_id] = self._bump()
        return self._applied(ch.channel_id)

    async def channel_tune(self, ch: wc.Channel, timeout: float = 5.0) -> wc.ChannelApplied:
        self.channel_calls.append(("tune", ch.channel_id, ch.offset_hz))
        await self._channel_step(timeout)
        old = self.channels.get(ch.channel_id)
        if old is None:
            raise WorkerRejected(wc.Result.E_NOT_FOUND)
        if abs(ch.offset_hz) > self.rate // 2:
            raise WorkerRejected(wc.Result.E_OUT_OF_RANGE)
        if (old.mode, old.agc, old.pass_lo_hz, old.pass_hi_hz, old.bfo_hz) != \
                (ch.mode, ch.agc, ch.pass_lo_hz, ch.pass_hi_hz, ch.bfo_hz):
            self.channel_gens[ch.channel_id] = self._bump()
        self.channels[ch.channel_id] = ch
        return self._applied(ch.channel_id)

    async def channel_close(self, channel_id: int, timeout: float = 5.0) -> wc.ChannelApplied:
        self.channel_calls.append(("close", channel_id, None))
        await self._channel_step(timeout)
        if channel_id not in self.channels:
            raise WorkerRejected(wc.Result.E_NOT_FOUND)
        ap = self._applied(channel_id, False)
        del self.channels[channel_id]
        return ap

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
