"""ControlCore against a fake worker: A2 command lifecycle, A3 serialized queue,
A4 leases and job survival, permissions, preconditions, idempotency, and
snapshot/patch correctness. Real-worker integration is in test_core_integration.py.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque

import pytest

from k2websdr.core.service import ControlCore
from k2websdr.protocol import models as m
from k2websdr.protocol import worker_ctl as wc
from k2websdr.protocol.errors import ErrorCode

from core_helpers import CENTER, LISTENER, OPERATOR, RATE, Client, async_test, cmd, rx_config
from fake_worker import FakeWorker, fake_factory

RX = "replay-0"


async def make(script=None, *, gate=None, policy="deny", timeout=2.0, **cfg):
    core = ControlCore()
    rt = core.add_receiver(rx_config(RX, takeover_policy=policy, command_timeout=timeout, **cfg),
                           fake_factory(deque(script or []), gate=gate))
    await core.start()
    return core, rt


async def operator_with_lease(core) -> Client:
    c = Client(core, OPERATOR)
    await c.subscribe()
    ack, _ = await c.outcome(await c.send("receiver.claim", receiver=RX))
    assert ack.status == "accepted", ack
    return c


# ------------------------------------------------------------ A2: lifecycle

@async_test
async def test_accepted_command_is_pending_until_worker_applies():
    gate = asyncio.Event()
    core, rt = await make([("apply",)], gate=gate)
    c = await operator_with_lease(core)
    gen0 = rt.capture_gen
    cid = await c.send("receiver.set_capture", receiver=RX, center_hz=CENTER)
    await asyncio.sleep(0.05)
    acks = [x for x in c.replies(cid) if isinstance(x, m.Ack)]
    assert len(acks) == 1 and acks[0].status == "accepted" and acks[0].rev == core.store.rev
    f = c.field(RX, "center_hz")
    assert (f["status"], f["cmd"], f["requested"], f["applied"]) == ("pending", cid, CENTER, CENTER)
    assert not [x for x in c.replies(cid) if isinstance(x, m.Result)], "no result before worker replies"
    gate.set()
    ack, res = await c.outcome(cid)
    assert res.status == "applied" and res.capture_gen == gen0 + 1 and res.code is None
    f = c.field(RX, "center_hz")
    assert (f["status"], f["applied"], f["verified"], f["error"]) == ("applied", CENTER, "driver_readback", None)
    assert c.mirror["receivers"][RX]["capture_gen"] == gen0 + 1
    await core.shutdown()


@async_test
async def test_applied_reports_readback_not_request():
    # Fake hardware lands 3 Hz away from the request (e.g. synthesizer step size).
    core, rt = await make([("apply", CENTER + 3, RATE)])
    c = await operator_with_lease(core)
    _, res = await c.outcome(await c.send("receiver.set_capture", receiver=RX, center_hz=CENTER))
    f = c.field(RX, "center_hz")
    assert (f["requested"], f["applied"], res.status) == (CENTER, CENTER + 3, "applied")
    await core.shutdown()


@pytest.mark.parametrize("step,code", [
    (("reject", wc.Result.E_UNSUPPORTED), ErrorCode.E_UNSUPPORTED),
    (("reject", wc.Result.E_OUT_OF_RANGE), ErrorCode.E_OUT_OF_RANGE),
    (("fail", wc.Result.E_DEVICE), ErrorCode.E_DEVICE_FAILED),
    (("fail", wc.Result.E_TIMEOUT), ErrorCode.E_TIMEOUT),
])
@async_test
async def test_worker_failure_keeps_last_applied_value(step, code):
    core, rt = await make([step])
    c = await operator_with_lease(core)
    gen0 = rt.capture_gen
    ack, res = await c.outcome(await c.send("receiver.set_capture", receiver=RX, center_hz=CENTER))
    assert ack.status == "accepted"
    assert (res.status, res.code) == ("failed", code)
    f = c.field(RX, "center_hz")
    assert (f["status"], f["error"], f["applied"]) == ("failed", code.value, CENTER)
    assert rt.capture_gen == gen0
    await core.shutdown()


@async_test
async def test_timeout_then_late_readback_updates_applied_but_not_outcome():
    core, rt = await make([("hang",)], timeout=0.2)
    c = await operator_with_lease(core)
    _, res = await c.outcome(await c.send("receiver.set_capture", receiver=RX, center_hz=CENTER))
    assert (res.status, res.code) == ("failed", ErrorCode.E_TIMEOUT)
    gen_before = rt.capture_gen
    FakeWorker.instances[-1].late_apply(CENTER, RATE)
    f = c.field(RX, "center_hz")
    assert f["status"] == "failed" and f["error"] == "E_TIMEOUT"   # the reported outcome stays true
    assert c.mirror["receivers"][RX]["capture_gen"] == gen_before + 1  # but the device state is tracked
    await core.shutdown()


@async_test
async def test_unacknowledged_value_never_appears_applied():
    """§13: across every state revision a client saw, `applied` only changed at APPLIED."""
    gate = asyncio.Event()
    core, rt = await make([("apply", CENTER, RATE)], gate=gate)
    c = await operator_with_lease(core)
    # Make the pre-command applied value distinguishable from the request.
    with core.store.txn() as t:
        t.replace("receivers", RX, "capture", "center_hz", "applied", value=CENTER - 1)
    c.drain()
    start = len(c.history)                         # first state after the setup change
    cid = await c.send("receiver.set_capture", receiver=RX, center_hz=CENTER)
    await asyncio.sleep(0.05)
    c.drain()
    seen_before_release = len(c.history)
    assert seen_before_release > start, "the pending state was published"
    gate.set()
    await c.outcome(cid)
    for state in c.history[start - 1:seen_before_release]:
        assert state["receivers"][RX]["capture"]["center_hz"]["applied"] == CENTER - 1
    assert c.history[-1]["receivers"][RX]["capture"]["center_hz"]["applied"] == CENTER
    await core.shutdown()


@async_test
async def test_worker_crash_mid_command_fails_it_and_restarts_with_higher_generation():
    core, rt = await make([("crash", 139)], restart_backoff=0.01)
    c = await operator_with_lease(core)
    gen0 = rt.capture_gen
    _, res = await c.outcome(await c.send("receiver.set_capture", receiver=RX, center_hz=CENTER))
    assert (res.status, res.code) == ("failed", ErrorCode.E_DEVICE_FAILED)
    for _ in range(100):
        await asyncio.sleep(0.01)
        if c.drain() is not None and c.mirror["receivers"][RX]["acquisition"]["state"] == "running":
            break
    assert c.mirror["receivers"][RX]["acquisition"]["state"] == "running"
    assert rt.capture_gen == gen0 + 1 and FakeWorker.instances[-1].gen == gen0 + 1
    states = [h["receivers"][RX]["acquisition"]["state"] for h in c.history]
    assert "failed" in states, "the crash was visible"
    await core.shutdown()


@async_test
async def test_restarts_are_bounded():
    core, rt = await make(max_restarts=2, restart_backoff=0.01)
    c = Client(core)
    await c.subscribe()
    for _ in range(3):
        rt.on_exit(1)
        await asyncio.sleep(0.1)
    c.drain()
    assert rt.restarts == 2
    assert c.mirror["receivers"][RX]["acquisition"]["state"] == "failed"
    await core.shutdown()


# --------------------------------------------------------- A3: serialization

@async_test
async def test_commands_execute_one_at_a_time_in_submission_order():
    gate = asyncio.Event()
    core, rt = await make([("apply",), ("apply",), ("apply",)], gate=gate)
    c = await operator_with_lease(core)
    ids = [await c.send("receiver.set_capture", receiver=RX, center_hz=CENTER),
           await c.send("receiver.set_capture", receiver=RX, sample_rate_hz=RATE),
           await c.send("receiver.set_capture", receiver=RX, center_hz=CENTER, sample_rate_hz=RATE)]
    await asyncio.sleep(0.05)
    fw = FakeWorker.instances[-1]
    assert len(fw.calls) == 1, "second command must wait for the first"
    gate.set()
    results = [(await c.outcome(i))[1] for i in ids]
    assert rt.executed == ids
    assert fw.calls == [(CENTER, None), (None, RATE), (CENTER, RATE)]
    assert fw.max_active == 1
    gens = [r.capture_gen for r in results]
    assert gens == sorted(gens) and len(set(gens)) == 3
    await core.shutdown()


@async_test
async def test_later_pending_command_keeps_its_field_pending_when_earlier_one_applies():
    gate = asyncio.Event()
    core, rt = await make([("apply",), ("apply",)], gate=gate)
    c = await operator_with_lease(core)
    first = await c.send("receiver.set_capture", receiver=RX, center_hz=CENTER)
    second = await c.send("receiver.set_capture", receiver=RX, center_hz=CENTER)
    f = c.field(RX, "center_hz")
    assert f["cmd"] == second and f["status"] == "pending"
    gate.set()
    await c.outcome(first)
    await c.outcome(second)
    f = c.field(RX, "center_hz")
    assert f["cmd"] == second and f["status"] == "applied"
    await core.shutdown()


# ------------------------------------------------------ A4: leases and jobs

@async_test
async def test_set_capture_requires_the_lease():
    core, rt = await make()
    c = Client(core)
    ack, _ = await c.outcome(await c.send("receiver.set_capture", receiver=RX, center_hz=CENTER))
    assert (ack.status, ack.code) == ("rejected", ErrorCode.E_NOT_LEASE_HOLDER)
    assert FakeWorker.instances[-1].calls == []
    await core.shutdown()


@async_test
async def test_job_lease_survives_browser_disconnect():
    core, rt = await make()
    job = core.start_stub_job(RX)
    a = Client(core)
    await a.subscribe()
    ack, _ = await a.outcome(await a.send("receiver.claim", receiver=RX))
    assert (ack.status, ack.code) == ("rejected", ErrorCode.E_BUSY)
    core.close_session(a.session)
    # A different browser later still sees the job running and holding the receiver.
    b = Client(core)
    await b.subscribe()
    assert b.mirror["jobs"][job]["state"] == "running" and b.mirror["jobs"][job]["holds_lease"] is True
    assert b.mirror["receivers"][RX]["lease"]["holder"] == {"kind": "job", "principal": "job:stub",
                                                            "session": None, "job": job}
    await core.shutdown()


@async_test
async def test_operator_lease_ends_with_its_session_but_job_leases_do_not():
    core, rt = await make()
    a = await operator_with_lease(core)
    watcher = Client(core, LISTENER)
    await watcher.subscribe()
    core.close_session(a.session)
    watcher.drain()
    assert watcher.mirror["receivers"][RX]["lease"]["holder"] is None
    job = core.start_stub_job(RX)
    core.close_session(watcher.session)
    assert rt.lease.holder.job == job
    await core.shutdown()


@async_test
async def test_takeover_refused_when_receiver_policy_is_deny():
    core, rt = await make(policy="deny")
    job = core.start_stub_job(RX)
    c = Client(core)
    await c.subscribe()
    ack, _ = await c.outcome(await c.send("receiver.claim", receiver=RX, takeover="preempt_with_notice"))
    assert (ack.status, ack.code) == ("rejected", ErrorCode.E_BUSY) and "policy is deny" in ack.detail
    assert c.mirror["jobs"][job]["state"] == "running"
    await core.shutdown()


@async_test
async def test_takeover_with_notice_is_predictable_and_visible():
    core, rt = await make(policy="preempt_with_notice")
    job = core.start_stub_job(RX)
    watcher = Client(core, LISTENER)
    await watcher.subscribe()
    op = Client(core)
    await op.subscribe()
    # Without asking for preemption, the claim is refused even under this policy.
    ack, _ = await op.outcome(await op.send("receiver.claim", receiver=RX))
    assert ack.code is ErrorCode.E_BUSY
    ack, res = await op.outcome(await op.send("receiver.claim", receiver=RX, takeover="preempt_with_notice"))
    assert ack.status == "accepted" and res.status == "applied"
    watcher.drain()
    j = watcher.mirror["jobs"][job]
    assert (j["state"], j["holds_lease"], j["detail"]) == ("preempted", False, "preempted by user:adam")
    assert watcher.mirror["receivers"][RX]["lease"]["holder"]["session"] == op.session.id
    # The preempted job did not silently re-take the receiver.
    assert rt.lease.holder.kind == "operator"
    await core.shutdown()


@async_test
async def test_stop_job_releases_lease():
    core, rt = await make()
    job = core.start_stub_job(RX)
    core.stop_job(job)
    assert rt.lease.holder is None
    c = Client(core)
    await c.subscribe()
    assert c.mirror["jobs"][job]["state"] == "stopped"
    await core.shutdown()


@async_test
async def test_release_requires_holder_and_reclaim_is_noop():
    core, rt = await make()
    a = await operator_with_lease(core)
    ack, _ = await a.outcome(await a.send("receiver.claim", receiver=RX))
    assert ack.status == "accepted"                              # re-claim: no-op
    b = Client(core)
    ack, _ = await b.outcome(await b.send("receiver.release", receiver=RX))
    assert ack.code is ErrorCode.E_NOT_LEASE_HOLDER
    await core.shutdown()


# ------------------------------------------------ permissions & preconditions

@pytest.mark.parametrize("op,args", [
    ("receiver.claim", {"receiver": RX}),
    ("receiver.set_capture", {"receiver": RX, "center_hz": CENTER}),
    ("receiver.set_antenna", {"receiver": RX, "port": "A"}),
])
@async_test
async def test_listener_is_forbidden_hardware_ops(op, args):
    core, rt = await make()
    c = Client(core, LISTENER)
    ack, _ = await c.outcome(await c.send(op, **args))
    assert (ack.status, ack.code) == ("rejected", ErrorCode.E_FORBIDDEN)
    await core.shutdown()


@async_test
async def test_preconditions_and_ranges_are_checked_before_acceptance():
    core, rt = await make()
    c = await operator_with_lease(core)
    cases = [
        ({"center_hz": CENTER + 1}, ErrorCode.E_OUT_OF_RANGE),
        ({"sample_rate_hz": RATE * 2}, ErrorCode.E_OUT_OF_RANGE),
        ({"center_hz": CENTER, "if_capture_gen": rt.capture_gen + 5}, ErrorCode.E_STALE_PRECONDITION),
    ]
    for args, code in cases:
        ack, _ = await c.outcome(await c.send("receiver.set_capture", receiver=RX, **args))
        assert (ack.status, ack.code) == ("rejected", code), args
    ack, _ = await c.outcome(await c.send("receiver.set_capture", receiver="nope", center_hz=CENTER))
    assert ack.code is ErrorCode.E_UNKNOWN_RECEIVER
    ack, _ = await c.outcome(await c.send("receiver.set_antenna", receiver=RX, port="A"))
    assert ack.code is ErrorCode.E_UNSUPPORTED
    assert FakeWorker.instances[-1].calls == []
    ack, res = await c.outcome(await c.send("receiver.set_capture", receiver=RX, center_hz=CENTER,
                                            if_capture_gen=rt.capture_gen))
    assert res.status == "applied"
    await core.shutdown()


@async_test
async def test_commands_refused_while_acquisition_not_running():
    core, rt = await make(max_restarts=0)
    c = await operator_with_lease(core)
    rt.on_exit(1)
    ack, _ = await c.outcome(await c.send("receiver.set_capture", receiver=RX, center_hz=CENTER))
    assert (ack.status, ack.code) == ("rejected", ErrorCode.E_DEVICE_FAILED)
    await core.shutdown()


@async_test
async def test_no_mode_is_available_in_m0():
    core, rt = await make()
    c = Client(core)
    ack, _ = await c.outcome(await c.send("channel.open", receiver=RX, frequency_hz=CENTER, mode="usb"))
    assert ack.code is ErrorCode.E_MODE_UNAVAILABLE
    await core.shutdown()


# ---------------------------------------------------------- idempotency

@async_test
async def test_retry_with_same_id_does_not_execute_twice():
    core, rt = await make([("apply",), ("apply",)])
    c = await operator_with_lease(core)
    cid, raw = cmd("receiver.set_capture", receiver=RX, center_hz=CENTER)
    await c.send_raw(raw)
    _, first = await c.outcome(cid)
    await c.send_raw(raw)
    c.drain()
    replies = [x for x in c.log if isinstance(x, (m.Ack, m.Result)) and x.id == cid]
    assert [type(x).__name__ for x in replies] == ["Ack", "Result", "Ack", "Result"]
    assert replies[3] == first
    assert len(FakeWorker.instances[-1].calls) == 1
    await core.shutdown()


@async_test
async def test_retry_from_a_new_session_after_disconnect_gets_the_result():
    gate = asyncio.Event()
    core, rt = await make([("apply",)], gate=gate)
    a = await operator_with_lease(core)
    cid, raw = cmd("receiver.set_capture", receiver=RX, center_hz=CENTER)
    await a.send_raw(raw)
    core.close_session(a.session)                 # browser suspended mid-command
    b = Client(core, OPERATOR)
    await b.send_raw(raw)                         # reconnect and retry the same id
    gate.set()
    ack, res = await b.outcome(cid)
    assert ack.status == "accepted" and res.status == "applied"
    assert len(FakeWorker.instances[-1].calls) == 1
    await core.shutdown()


# ------------------------------------------------------- malformed input

@async_test
async def test_unparseable_input_gets_error_message():
    core, rt = await make()
    c = Client(core)
    await c.send_raw("not json")
    (msg,) = c.drain()
    assert isinstance(msg, m.ErrorMsg) and msg.code is ErrorCode.E_SCHEMA
    _, raw = cmd("receiver.set_capture", receiver=RX, center_hz=7074000.0)
    await c.send_raw(raw)
    (msg,) = c.drain()
    assert isinstance(msg, m.Ack) and msg.status == "rejected" and msg.code is ErrorCode.E_SCHEMA
    await core.shutdown()


# ------------------------------------------------------- snapshot / patch

@async_test
async def test_client_mirror_equals_server_state_after_activity():
    core, rt = await make([("apply",), ("fail", wc.Result.E_DEVICE), ("apply",)], policy="preempt_with_notice")
    c = await operator_with_lease(core)
    w = Client(core, LISTENER)
    await w.subscribe()
    for _ in range(3):
        await c.outcome(await c.send("receiver.set_capture", receiver=RX, center_hz=CENTER))
    core.close_session(c.session)
    job = core.start_stub_job(RX)
    core.stop_job(job)
    w.drain()
    assert w.mirror == core.store.view()
    assert w.rev == core.store.rev
    revs = [x.rev for x in w.log if isinstance(x, m.Patch)]
    assert revs == list(range(revs[0], revs[0] + len(revs)))
    await core.shutdown()


@async_test
async def test_every_published_state_is_valid():
    core, rt = await make([("apply",), ("reject", wc.Result.E_UNSUPPORTED)])
    c = await operator_with_lease(core)
    await c.outcome(await c.send("receiver.set_capture", receiver=RX, center_hz=CENTER))
    await c.outcome(await c.send("receiver.set_capture", receiver=RX, center_hz=CENTER))
    for state in c.history:
        m.ServerState.model_validate_json(json.dumps(state))
    await core.shutdown()
