"""Listener channels in the control core: channel.open / channel.tune / channel.close (M1 step 3b-1).

Driven through ControlCore with the scriptable fake worker (same channel semantics as the real
worker, docs/protocol/worker-dsp-v1.md). Fixture receiver: two_tone_ci16, 48 kS/s complex
around 7.074 MHz -> AM/USB/LSB/CW/NFM fit, WFM (192 kS/s) does not.
"""

from __future__ import annotations

import asyncio
from collections import deque

from k2websdr.core.service import MAX_CHANNEL_CMDS_PER_SESSION, MAX_CHANNELS_PER_SESSION, ControlCore
from k2websdr.protocol import models as m
from k2websdr.protocol import worker_ctl as wc
from k2websdr.protocol.errors import ErrorCode

from core_helpers import CENTER, LISTENER, OPERATOR, RATE, Client, async_test, rx_config
from fake_worker import FakeWorker, fake_factory

RX = "replay-0"
M1 = ["am", "usb", "lsb", "cw", "nfm", "wfm"]


async def make(*, channel_script=None, channel_gate=None, script=None, available=M1, timeout=2.0):
    FakeWorker.instances.clear()
    core = ControlCore(ledger=m.LedgerSummary(version=1, available=available))
    rt = core.add_receiver(rx_config(RX, command_timeout=timeout),
                           fake_factory(deque(script or []), channel_script=channel_script,
                                        channel_gate=channel_gate))
    await core.start()
    return core, rt


def worker(rt) -> FakeWorker:
    return rt.link


async def open_channel(c: Client, freq=CENTER + 1000, mode="usb") -> m.Result:
    ack, res = await c.outcome(await c.send("channel.open", receiver=RX, frequency_hz=freq, mode=mode))
    assert ack.status == "accepted", ack
    assert res is not None and res.status == "applied", res
    return res


@async_test
async def test_open_needs_only_listen_and_no_lease_and_publishes_state():
    core, rt = await make()
    c = Client(core, LISTENER)
    await c.subscribe()
    res = await open_channel(c, CENTER + 1500, "usb")
    assert res.channel and res.channel_gen >= 1
    st = c.mirror["channels"][res.channel]
    assert st == {"receiver": RX, "owner": {"session": c.session.id, "job": None}, "frequency_hz": CENTER + 1500,
                  "mode": "usb", "channel_gen": res.channel_gen, "decoder_taps": []}
    assert worker(rt).channel_calls == [("open", int(res.channel[3:]), 1500)]   # offset from the centre, integer Hz
    assert rt.lease.holder is None
    await core.shutdown()


@async_test
async def test_mode_availability_uses_ledger_and_receiver_bandwidth():
    core, _ = await make(available=["usb", "wfm"])
    c = Client(core, LISTENER)
    ack, _ = await c.outcome(await c.send("channel.open", receiver=RX, frequency_hz=CENTER, mode="am"))
    assert ack.code is ErrorCode.E_MODE_UNAVAILABLE and "not available" in ack.detail
    ack, _ = await c.outcome(await c.send("channel.open", receiver=RX, frequency_hz=CENTER, mode="wfm"))
    assert ack.code is ErrorCode.E_MODE_UNAVAILABLE and "192000" in ack.detail             # 48 kS/s capture
    await core.shutdown()


@async_test
async def test_frequency_outside_the_capture_span_is_out_of_range_with_a_hint():
    core, _ = await make()
    c = Client(core, LISTENER)
    ack, _ = await c.outcome(await c.send("channel.open", receiver=RX, frequency_hz=CENTER + RATE // 2, mode="usb"))
    assert ack.code is ErrorCode.E_OUT_OF_RANGE and "tune between" in ack.detail
    ack, _ = await c.outcome(await c.send("channel.open", receiver="nope", frequency_hz=CENTER, mode="usb"))
    assert ack.code is ErrorCode.E_UNKNOWN_RECEIVER
    await core.shutdown()


@async_test
async def test_listen_permission_is_required():
    core, _ = await make()
    from k2websdr.core.service import Principal
    c = Client(core, Principal("user:none", frozenset()))
    ack, _ = await c.outcome(await c.send("channel.open", receiver=RX, frequency_hz=CENTER, mode="usb"))
    assert ack.code is ErrorCode.E_FORBIDDEN
    await core.shutdown()


@async_test
async def test_tune_frequency_keeps_generation_mode_change_bumps_it():
    core, rt = await make()
    c = Client(core, LISTENER)
    await c.subscribe()
    first = await open_channel(c, CENTER + 1000, "usb")
    _, r1 = await c.outcome(await c.send("channel.tune", channel=first.channel, frequency_hz=CENTER + 2000))
    assert r1.status == "applied" and r1.channel_gen == first.channel_gen
    _, r2 = await c.outcome(await c.send("channel.tune", channel=first.channel, mode="lsb"))
    assert r2.status == "applied" and r2.channel_gen > first.channel_gen
    st = c.mirror["channels"][first.channel]
    assert (st["frequency_hz"], st["mode"], st["channel_gen"]) == (CENTER + 2000, "lsb", r2.channel_gen)
    assert [x[0] for x in worker(rt).channel_calls] == ["open", "tune", "tune"]
    await core.shutdown()


@async_test
async def test_if_channel_gen_precondition():
    core, _ = await make()
    c = Client(core, LISTENER)
    first = await open_channel(c)
    ack, _ = await c.outcome(await c.send("channel.tune", channel=first.channel, mode="am",
                                          if_channel_gen=first.channel_gen + 5))
    assert ack.code is ErrorCode.E_STALE_PRECONDITION
    _, res = await c.outcome(await c.send("channel.tune", channel=first.channel, mode="am",
                                          if_channel_gen=first.channel_gen))
    assert res.status == "applied"
    await core.shutdown()


@async_test
async def test_only_the_owner_session_may_tune_or_close():
    core, _ = await make()
    a, b = Client(core, LISTENER), Client(core, OPERATOR)
    res = await open_channel(a)
    for op, args in (("channel.tune", {"frequency_hz": CENTER}), ("channel.close", {})):
        ack, _ = await b.outcome(await b.send(op, channel=res.channel, **args))
        assert ack.code is ErrorCode.E_FORBIDDEN, (op, ack)
    ack, _ = await a.outcome(await a.send("channel.close", channel="ch_999"))
    assert ack.code is ErrorCode.E_UNKNOWN_CHANNEL
    await core.shutdown()


@async_test
async def test_close_removes_state_and_the_worker_channel():
    core, rt = await make()
    c = Client(core, LISTENER)
    await c.subscribe()
    res = await open_channel(c)
    _, r = await c.outcome(await c.send("channel.close", channel=res.channel))
    assert r.status == "applied" and r.channel == res.channel
    c.drain()
    assert res.channel not in c.mirror["channels"] and not worker(rt).channels
    ack, _ = await c.outcome(await c.send("channel.tune", channel=res.channel, mode="am"))
    assert ack.code is ErrorCode.E_UNKNOWN_CHANNEL
    await core.shutdown()


@async_test
async def test_session_end_closes_its_channels_only():
    core, rt = await make()
    a, b = Client(core, LISTENER), Client(core, LISTENER)
    ra, rb = await open_channel(a), await open_channel(b, CENTER - 1000, "lsb")
    core.close_session(a.session)
    await asyncio.sleep(0.05)
    assert set(core.store.view()["channels"]) == {rb.channel}
    assert set(worker(rt).channels) == {int(rb.channel[3:])}
    await core.shutdown()


@async_test
async def test_per_session_channel_limit():
    core, _ = await make()
    c = Client(core, LISTENER)
    for i in range(MAX_CHANNELS_PER_SESSION):
        await open_channel(c, CENTER + 100 * i)
    ack, _ = await c.outcome(await c.send("channel.open", receiver=RX, frequency_hz=CENTER, mode="usb"))
    assert ack.code is ErrorCode.E_BUSY and "limit" in ack.detail
    await core.shutdown()


@async_test
async def test_channel_command_cap_rejects_with_e_busy_without_recording():
    gate = asyncio.Event()
    core, _ = await make(channel_gate=gate)
    c = Client(core, LISTENER)
    opened = []
    gate.set()
    for i in range(2):
        opened.append(await open_channel(c, CENTER + 100 * i))
    gate.clear()
    cids = [await c.send("channel.tune", channel=opened[i % 2].channel, frequency_hz=CENTER + 10 * i)
            for i in range(MAX_CHANNEL_CMDS_PER_SESSION)]
    assert all(c.replies(x)[0].status == "accepted" for x in cids)
    over = await c.send("channel.tune", channel=opened[0].channel, frequency_hz=CENTER)
    ack, _ = await c.outcome(over)
    assert ack.code is ErrorCode.E_BUSY
    assert (c.session.principal.user, over) not in core._cmds                      # capacity: not recorded
    gate.set()
    for x in cids:
        assert (await c.outcome(x))[1].status == "applied"
    await core.shutdown()


@async_test
async def test_worker_refusal_becomes_a_failed_result_and_frees_the_slot():
    core, rt = await make(channel_script=deque([("reject", wc.Result.E_OUT_OF_RANGE)]))
    c = Client(core, LISTENER)
    await c.subscribe()
    ack, res = await c.outcome(await c.send("channel.open", receiver=RX, frequency_hz=CENTER, mode="usb"))
    assert ack.status == "accepted" and res.status == "failed" and res.code is ErrorCode.E_OUT_OF_RANGE
    c.drain()
    assert c.mirror["channels"] == {} and core._channels == {}
    await open_channel(c)                                                          # next one works
    await core.shutdown()


@async_test
async def test_worker_timeout_fails_the_open_with_e_timeout():
    core, _ = await make(channel_script=deque([("hang",)]), timeout=0.2)
    c = Client(core, LISTENER)
    _, res = await c.outcome(await c.send("channel.open", receiver=RX, frequency_hz=CENTER, mode="usb"))
    assert res.status == "failed" and res.code is ErrorCode.E_TIMEOUT
    assert core._channels == {}
    await core.shutdown()


@async_test
async def test_session_ending_while_open_is_in_flight_undoes_it_in_the_worker():
    gate = asyncio.Event()
    core, rt = await make(channel_gate=gate)
    c = Client(core, LISTENER)
    cid = await c.send("channel.open", receiver=RX, frequency_hz=CENTER, mode="usb")
    core.close_session(c.session)
    gate.set()
    await asyncio.sleep(0.1)
    assert core.store.view()["channels"] == {} and worker(rt).channels == {}
    await core.shutdown()


@async_test
async def test_idempotent_retry_of_channel_open_does_not_open_twice():
    core, rt = await make()
    c = Client(core, LISTENER)
    cid, raw = __import__("core_helpers").cmd("channel.open", receiver=RX, frequency_hz=CENTER, mode="usb")
    await c.send_raw(raw)
    _, r1 = await c.outcome(cid)
    await c.send_raw(raw)
    await asyncio.sleep(0.05)
    results = [x for x in c.replies(cid) if isinstance(x, m.Result)]
    assert len(results) == 2 and results[0] == results[1]
    assert sum(1 for x in worker(rt).channel_calls if x[0] == "open") == 1
    await core.shutdown()


# ------------------------------------------------- capture changes and worker restarts

@async_test
async def test_capture_reacquisition_keeps_channels_on_their_frequency_with_new_generation():
    core, rt = await make()
    op = Client(core, OPERATOR)
    await op.subscribe()
    await op.outcome(await op.send("receiver.claim", receiver=RX))
    res = await open_channel(op, CENTER + 1000)
    _, r = await op.outcome(await op.send("receiver.set_capture", receiver=RX, center_hz=CENTER))
    assert r.status == "applied"
    await asyncio.sleep(0.05)
    op.drain()
    st = op.mirror["channels"][res.channel]
    assert st["frequency_hz"] == CENTER + 1000 and st["channel_gen"] > res.channel_gen
    assert worker(rt).channel_calls[-1] == ("tune", int(res.channel[3:]), 1000)
    await core.shutdown()


@async_test
async def test_centre_move_retunes_channels_that_fit_and_closes_the_rest():
    core, rt = await make(script=[("apply", CENTER + 20_000, RATE)])        # fake hardware moved the centre
    op = Client(core, OPERATOR)
    await op.subscribe()
    await op.outcome(await op.send("receiver.claim", receiver=RX))
    near = await open_channel(op, CENTER + 15_000)                        # still fits: offset -5000
    far = await open_channel(op, CENTER - 15_000, "lsb")                  # would be 35 kHz below: gone
    await op.outcome(await op.send("receiver.set_capture", receiver=RX, center_hz=CENTER))
    await asyncio.sleep(0.05)
    op.drain()
    assert set(op.mirror["channels"]) == {near.channel}
    assert op.mirror["channels"][near.channel]["frequency_hz"] == CENTER + 15_000
    assert worker(rt).channels[int(near.channel[3:])].offset_hz == -5000
    assert int(far.channel[3:]) not in worker(rt).channels
    await core.shutdown()


@async_test
async def test_worker_restart_reopens_channels():
    core, rt = await make(script=[("crash", 70)])
    rt.cfg.restart_backoff = 0.01
    op = Client(core, OPERATOR)
    await op.subscribe()
    await op.outcome(await op.send("receiver.claim", receiver=RX))
    res = await open_channel(op, CENTER + 700, "cw")
    await op.outcome(await op.send("receiver.set_capture", receiver=RX, center_hz=CENTER))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if rt.link is not None and rt.link is not FakeWorker.instances[0] and rt.link.channels:
            break
    new = rt.link
    assert new is not FakeWorker.instances[0]
    assert new.channel_calls == [("open", int(res.channel[3:]), 700)]
    op.drain()
    assert op.mirror["channels"][res.channel]["channel_gen"] >= 1
    await core.shutdown()
