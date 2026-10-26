"""ControlCore with real ksdr-worker processes (replay).

Covers A2 end to end, A7 (stale-generation decoder events never get through
after a retune), worker crash recovery with a monotonic generation, lock
contention, and clean shutdown.
"""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import datetime, timezone

from k2websdr.core.receiver import replay_link_factory
from k2websdr.core.service import ControlCore
from k2websdr.protocol import models as m
from k2websdr.protocol.decoder import DecoderEvent, EngineInfo
from k2websdr.protocol.errors import ErrorCode
from k2websdr.protocol.frame import FrameFlags

from core_helpers import CENTER, OPERATOR, Client, async_test, rx_config
from worker_harness import WORKER_BIN

RX = "replay-int"


async def make(tmp_path, **cfg) -> ControlCore:
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(exist_ok=True)
    core = ControlCore()
    core.add_receiver(rx_config(RX, frame_samples=1200, **cfg), replay_link_factory(WORKER_BIN, lock_dir))
    await core.start()
    return core


@async_test
async def test_set_capture_through_real_worker(tmp_path):
    core = await make(tmp_path)
    rt = core.receivers[RX]
    c = Client(core, OPERATOR)
    await c.subscribe()
    assert c.mirror["receivers"][RX]["acquisition"]["state"] == "running"
    assert c.mirror["receivers"][RX]["capture_gen"] == 1
    await c.outcome(await c.send("receiver.claim", receiver=RX))
    ack, res = await c.outcome(await c.send("receiver.set_capture", receiver=RX, center_hz=CENTER))
    assert (ack.status, res.status, res.capture_gen) == ("accepted", "applied", 2)
    f = c.field(RX, "center_hz")
    assert (f["applied"], f["status"], f["verified"]) == (CENTER, "applied", "none")   # replay: no hardware readback
    pid = rt.link.pid
    await core.shutdown()
    assert not os.path.exists(f"/proc/{pid}") or open(f"/proc/{pid}/stat").read().split()[2] == "Z"


@async_test
async def test_stale_generation_events_never_get_through_after_retune(tmp_path):
    """A7: a slow decoder keeps emitting events for old-generation samples after the retune."""
    core = await make(tmp_path)
    rt = core.receivers[RX]
    # The gate also needs a current channel; channels arrive in M1, so register one directly.
    core.gate.set_channel_gen("ch_test", RX, 1)

    backlog: list[DecoderEvent] = []            # decoder input queue: decoding lags the stream
    released: list[tuple[int, bool, int]] = []  # (event gen, admitted, gate gen at submit)

    def on_frame(rid, hdr, payload):
        if hdr.sample_count == 0:
            return
        backlog.append(DecoderEvent(
            v=1, receiver=rid, capture_gen=hdr.capture_gen, channel="ch_test", channel_gen=1,
            sample_index=str(hdr.first_sample_index), utc=datetime.now(timezone.utc), mode="usb",
            raw=f"frame seq {hdr.seq}", engine=EngineInfo(name="fake", version="0")))

    rt.frame_listeners.append(on_frame)
    c = Client(core, OPERATOR)
    await c.outcome(await c.send("receiver.claim", receiver=RX))
    while len(backlog) < 5:                     # old-generation work piles up
        await asyncio.sleep(0.01)
    _, res = await c.outcome(await c.send("receiver.set_capture", receiver=RX, center_hz=CENTER))
    assert res.capture_gen == 2
    await asyncio.sleep(0.2)                    # new-generation frames arrive too
    # The decoder now finishes its backlog, oldest first.
    for ev in list(backlog):
        gate_gen = core.gate.capture_gen(RX)
        released.append((ev.capture_gen, core.submit_decoder_event(ev), gate_gen))
    old = [r for r in released if r[0] == 1]
    new = [r for r in released if r[0] == 2]
    assert old and new
    assert not any(admitted for _, admitted, _ in old), "an old-generation event got through"
    assert all(admitted for _, admitted, _ in new)
    assert core.gate.stats.dropped["stale_capture_gen"] == len(old)
    assert all(ev.capture_gen == 2 for ev in core.decoder_sink)
    await core.shutdown()


@async_test
async def test_worker_crash_restarts_with_monotonic_generation(tmp_path):
    core = await make(tmp_path, restart_backoff=0.05)
    rt = core.receivers[RX]
    gens_seen: list[int] = []
    new_gen_frames: list[int] = []

    def on_frame(rid, hdr, payload):
        gens_seen.append(hdr.capture_gen)
        if FrameFlags.NEW_GEN in hdr.flags:
            new_gen_frames.append(hdr.capture_gen)

    rt.frame_listeners.append(on_frame)
    while not gens_seen:
        await asyncio.sleep(0.01)
    old_pid = rt.link.pid
    os.kill(old_pid, signal.SIGKILL)
    for _ in range(300):
        await asyncio.sleep(0.01)
        if rt.link is not None and rt.link.pid != old_pid and rt.capture_gen == 2 and gens_seen[-1] == 2:
            break
    assert rt.restarts == 1
    assert rt.capture_gen == 2 and gens_seen[-1] == 2
    assert gens_seen == sorted(gens_seen), "generation never went backwards across the restart"
    assert new_gen_frames == [1, 2]
    c = Client(core)
    await c.subscribe()
    assert c.mirror["receivers"][RX]["acquisition"]["state"] == "running"
    await core.shutdown()


@async_test
async def test_second_core_on_same_device_reports_lock_busy(tmp_path):
    core_a = await make(tmp_path)
    core_b = ControlCore()
    core_b.add_receiver(rx_config(RX), replay_link_factory(WORKER_BIN, tmp_path / "locks"))
    try:
        await core_b.start()
    except Exception as e:  # noqa: BLE001
        assert "E_BUSY" in str(e)
    else:
        raise AssertionError("second core started on a locked device")
    c = Client(core_b)
    await c.subscribe()
    acq = c.mirror["receivers"][RX]["acquisition"]
    assert (acq["state"], acq["detail"]) == ("failed", "device locked by another process")
    # The first core is unaffected and still accepts commands.
    a = Client(core_a, OPERATOR)
    await a.outcome(await a.send("receiver.claim", receiver=RX))
    _, res = await a.outcome(await a.send("receiver.set_capture", receiver=RX, center_hz=CENTER))
    assert res.status == "applied"
    await core_a.shutdown()


@async_test
async def test_replay_cannot_retune_elsewhere(tmp_path):
    core = await make(tmp_path)
    c = Client(core, OPERATOR)
    await c.outcome(await c.send("receiver.claim", receiver=RX))
    ack, _ = await c.outcome(await c.send("receiver.set_capture", receiver=RX, center_hz=14_074_000))
    assert (ack.status, ack.code) == ("rejected", ErrorCode.E_OUT_OF_RANGE)
    await core.shutdown()
