"""Live Airspy HF+ receivers in the control core (M1 step 4b), with the fake worker.

receiver.set_hw_gain (lease + hw.retune, serialized with retunes, composes partial changes),
the gain field in the state, restarts that reopen where the device was, and the restart policy
for live devices (unbounded, capped backoff, busy vs lost).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

import pytest

from k2websdr.core.receiver import AirspyhfSpec, ReceiverConfig
from k2websdr.core.service import ControlCore
from k2websdr.protocol import models as m
from k2websdr.protocol import worker_ctl as wc
from k2websdr.protocol.errors import ErrorCode

from core_helpers import LISTENER, OPERATOR, Client, async_test
from fake_worker import FakeWorker, fake_factory

RX = "hfplus"
SERIAL = 0xA1B2C3D4E5F60718
DEFAULT_GAIN = {"hf_agc": True, "hf_agc_threshold": "low", "hf_lna": True, "hf_att": 0}


def spec(**kw) -> AirspyhfSpec:
    return AirspyhfSpec(**{"serial": SERIAL, "center_hz": 7_100_000, "rate_hz": 384000,
                           "freq_range_hz": (10_000, 30_000_000), **kw})


async def make(script=None, *, gate=None, **cfg):
    FakeWorker.instances.clear()
    core = ControlCore()
    rt = core.add_receiver(ReceiverConfig(receiver_id=RX, label="HF+", receiver_idx=0, airspyhf=spec(),
                                          command_timeout=2.0, **cfg),
                           fake_factory(deque(script or []), gate=gate))
    await core.start()
    return core, rt


async def operator(core) -> Client:
    c = Client(core, OPERATOR)
    await c.subscribe()
    ack, _ = await c.outcome(await c.send("receiver.claim", receiver=RX))
    assert ack.status == "accepted"
    return c


@async_test
async def test_live_receiver_state_after_start():
    core, _ = await make()
    c = Client(core, LISTENER)
    await c.subscribe()
    st = c.mirror["receivers"][RX]
    assert st["driver"] == "airspyhf" and st["acquisition"]["state"] == "running"
    assert st["capabilities"] == {"freq_range_hz": [10_000, 30_000_000],
                                  "sample_rates_hz": [912000, 768000, 650000, 456000, 384000, 228000, 192000],
                                  "antennas": [], "sample_format": "cf32"}
    cap = st["capture"]
    assert (cap["center_hz"]["applied"], cap["center_hz"]["status"], cap["center_hz"]["verified"]) == \
        (7_100_000, "applied", "none")                                 # set on open; no getter exists
    assert cap["sample_rate_hz"]["applied"] == 384000
    assert cap["hw_gain"]["applied"] == DEFAULT_GAIN and cap["hw_gain"]["verified"] == "none"
    await core.shutdown()


@async_test
async def test_set_hw_gain_needs_the_lease_and_hw_retune():
    core, _ = await make()
    lis = Client(core, LISTENER)
    ack, _ = await lis.outcome(await lis.send("receiver.set_hw_gain", receiver=RX, hf_att=3))
    assert ack.code is ErrorCode.E_FORBIDDEN
    op = Client(core, OPERATOR)
    ack, _ = await op.outcome(await op.send("receiver.set_hw_gain", receiver=RX, hf_att=3))
    assert ack.code is ErrorCode.E_NOT_LEASE_HOLDER
    await core.shutdown()


@async_test
async def test_partial_gain_change_merges_and_reaches_the_worker_in_full():
    core, rt = await make()
    c = await operator(core)
    ack, res = await c.outcome(await c.send("receiver.set_hw_gain", receiver=RX, hf_agc=False, hf_att=5))
    assert ack.status == "accepted" and res.status == "applied" and res.capture_gen == rt.capture_gen
    want = dict(DEFAULT_GAIN, hf_agc=False, hf_att=5)
    g = c.mirror["receivers"][RX]["capture"]["hw_gain"]
    assert (g["requested"], g["applied"], g["status"], g["cmd"]) == (want, want, "applied", ack.id)
    assert rt.link.hw_calls == [wc.SetHw(False, False, True, 5)]      # every control, every time
    assert rt.capture_gen == 1                                         # gain is not a new generation
    await core.shutdown()


@async_test
async def test_queued_gain_commands_compose_and_run_in_order_with_retunes():
    gate = asyncio.Event()
    core, rt = await make(gate=gate)
    c = await operator(core)
    a = await c.send("receiver.set_capture", receiver=RX, center_hz=9_000_000)
    b = await c.send("receiver.set_hw_gain", receiver=RX, hf_lna=False)
    d = await c.send("receiver.set_hw_gain", receiver=RX, hf_att=2)       # builds on b's pending wish
    await asyncio.sleep(0.05)
    assert c.mirror is not None
    c.drain()
    g = c.mirror["receivers"][RX]["capture"]["hw_gain"]
    assert g["status"] == "pending" and g["requested"] == dict(DEFAULT_GAIN, hf_lna=False, hf_att=2)
    gate.set()
    for cid in (a, b, d):
        _, res = await c.outcome(cid)
        assert res.status == "applied", res
    assert rt.executed == [a, b, d]
    assert rt.link.calls == [(9_000_000, None), ("hw", None), ("hw", None)]
    assert c.mirror["receivers"][RX]["capture"]["hw_gain"]["applied"] == dict(DEFAULT_GAIN, hf_lna=False, hf_att=2)
    await core.shutdown()


@async_test
async def test_driver_failure_marks_the_gain_failed_and_keeps_the_applied_value():
    core, _ = await make([("fail", wc.Result.E_DEVICE)])
    c = await operator(core)
    _, res = await c.outcome(await c.send("receiver.set_hw_gain", receiver=RX, hf_att=4))
    assert res.status == "failed" and res.code is ErrorCode.E_DEVICE_FAILED
    g = c.mirror["receivers"][RX]["capture"]["hw_gain"]
    assert (g["status"], g["error"], g["applied"]) == ("failed", "E_DEVICE_FAILED", DEFAULT_GAIN)
    await core.shutdown()


@async_test
async def test_replay_receivers_refuse_hw_gain():
    from core_helpers import rx_config
    core = ControlCore()
    core.add_receiver(rx_config("replay-0"), fake_factory())
    await core.start()
    c = Client(core, OPERATOR)
    await c.outcome(await c.send("receiver.claim", receiver="replay-0"))
    ack, _ = await c.outcome(await c.send("receiver.set_hw_gain", receiver="replay-0", hf_att=1))
    assert ack.code is ErrorCode.E_UNSUPPORTED
    await core.shutdown()


@pytest.mark.parametrize("args", [{}, {"hf_att": 9}, {"hf_att": -1}, {"hf_agc_threshold": "mid"}, {"hf_agc": 1}])
def test_set_hw_gain_arguments_are_validated(args):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        m.ReceiverSetHwGainArgs(receiver=RX, **args)


@async_test
async def test_a_restarted_worker_opens_where_the_device_was():
    core, rt = await make(restart_backoff=0.01)
    c = await operator(core)
    await c.outcome(await c.send("receiver.set_capture", receiver=RX, center_hz=14_074_000, sample_rate_hz=192000))
    await c.outcome(await c.send("receiver.set_hw_gain", receiver=RX, hf_agc=False, hf_att=6))
    rt.link.alive = False
    rt.on_exit(69)                                                     # unplugged
    for _ in range(100):
        if len(FakeWorker.instances) == 2 and rt.link is not None:
            break
        await asyncio.sleep(0.01)
    center, rate, gain = FakeWorker.instances[1].opened_with
    assert (center, rate) == (14_074_000, 192000)
    assert gain == m.HwGain(hf_agc=False, hf_agc_threshold="low", hf_lna=True, hf_att=6)
    c.drain()
    st = c.mirror["receivers"][RX]
    assert st["acquisition"]["state"] == "running" and st["capture_gen"] > 2
    assert st["capture"]["center_hz"]["applied"] == 14_074_000
    await core.shutdown()


# ------------------------------------------------------------------------------ restart policy

def _detail(core) -> tuple[str, str]:
    a = core.store.view()["receivers"][RX]["acquisition"]
    return a["state"], a["detail"]


@async_test
async def test_live_receivers_retry_without_limit_with_capped_backoff():
    core, rt = await make(max_restarts=None, restart_backoff=1.0, restart_backoff_max=8.0)
    delays = []
    for _ in range(7):
        rt.on_exit(69)
        delays.append(rt._retry_in)
        rt._restart_task.cancel()
    assert delays == [1, 2, 4, 8, 8, 8, 8]
    assert _detail(core) == ("failed", "device not found or lost; retrying in 8 s")
    await core.shutdown()


@async_test
async def test_busy_device_is_external_busy_not_failed():
    core, rt = await make(max_restarts=None, restart_backoff=5.0)
    rt.on_exit(75)
    rt._restart_task.cancel()
    assert _detail(core) == ("external_busy", "device in use by another process; retrying in 5 s")
    await core.shutdown()


@async_test
async def test_a_long_healthy_run_resets_the_backoff():
    core, rt = await make(max_restarts=None, restart_backoff=1.0, healthy_after_s=0.05)
    rt.on_exit(69)
    rt._restart_task.cancel()
    rt.on_exit(69)
    rt._restart_task.cancel()
    assert rt._retry_in == 2
    rt._started_at = time.monotonic() - 1.0                           # that worker had run for 1 s
    rt.on_exit(69)
    rt._restart_task.cancel()
    assert rt._retry_in == 1                                           # a new incident, not a crash loop
    await core.shutdown()


@pytest.mark.parametrize("kw", [
    {"center_hz": 5_000},                                    # outside the safe range
    {"rate_hz": 500000},                                     # not a measured device rate
    {"sample_rates_hz": (384000, 999999)},
    {"freq_range_hz": (30_000_000, 10_000)},
    {"serial": 0},
])
def test_spec_validation(kw):
    with pytest.raises(ValueError):
        spec(**kw)
