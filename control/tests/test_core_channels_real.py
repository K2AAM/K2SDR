"""channel.open through ControlCore against a real ksdr-worker (replay of the real WWV clip):
the service's channel id, generation and absolute frequency match the AUDIO frames the worker sends."""

from __future__ import annotations

import asyncio

from k2websdr.core.receiver import ReceiverConfig, replay_link_factory
from k2websdr.core.service import ControlCore
from k2websdr.protocol import models as m
from k2websdr.protocol.errors import ErrorCode
from k2websdr.protocol.frame import FrameKind
from k2websdr.replay import sigmf

from core_helpers import LISTENER, ROOT, Client, async_test
from worker_harness import WORKER_BIN

WWV = sigmf.load(ROOT / "fixtures/real/airspyhf/airspyhf_wwv10_48k.sigmf-meta", require_sha512=True)


@async_test
async def test_service_channel_drives_real_worker_audio(tmp_path):
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    core = ControlCore(ledger=m.LedgerSummary(version=1, available=["am", "usb"]))
    rt = core.add_receiver(ReceiverConfig(receiver_id="wwv", label="wwv", receiver_idx=0, recording=WWV,
                                          paced=True, loop=True),
                           replay_link_factory(WORKER_BIN, lock_dir))
    audio = []
    rt.frame_listeners.append(lambda rid, h, p: audio.append(h) if h.kind is FrameKind.AUDIO else None)
    await core.start()
    try:
        c = Client(core, LISTENER)
        await c.subscribe()
        ack, res = await c.outcome(await c.send("channel.open", receiver="wwv", frequency_hz=10_000_000, mode="am"))
        assert ack.status == "accepted" and res.status == "applied", (ack, res)
        for _ in range(100):
            await asyncio.sleep(0.02)
            if len(audio) >= 3:
                break
        mine = [h for h in audio if h.stream_id == int(res.channel[3:])]
        assert mine, audio[:3]
        assert all(h.freq_origin_hz == 10_000_000 and h.audio_ext.channel_gen == res.channel_gen for h in mine)
        _, r = await c.outcome(await c.send("channel.close", channel=res.channel))
        assert r.status == "applied"
    finally:
        await core.shutdown()


RX = "wwv"


async def _start():
    import tempfile
    lock_dir = __import__("pathlib").Path(tempfile.mkdtemp())
    core = ControlCore(ledger=m.LedgerSummary(version=1, available=["am", "usb"]))
    rt = core.add_receiver(ReceiverConfig(receiver_id=RX, label=RX, receiver_idx=0, recording=WWV,
                                          paced=True, loop=True),
                           replay_link_factory(WORKER_BIN, lock_dir))
    await core.start()
    return core, rt


@async_test
async def test_real_worker_runs_the_requested_passband_and_refuses_one_too_wide():
    """The passband in the state is the worker's readback, and the audio carries it."""
    core, rt = await _start()
    c = Client(core, LISTENER)
    await c.subscribe()
    frames = []
    rt.frame_listeners.append(lambda rid, h, p: frames.append(h))
    ack, res = await c.outcome(await c.send("channel.open", receiver=RX, frequency_hz=10_000_000, mode="am",
                                            passband_hz=[-3000, 3000], agc="slow"))
    assert res.status == "applied", (ack, res)
    st = c.mirror["channels"][res.channel]
    assert (st["passband_hz"], st["agc"]) == ([-3000, 3000], "slow")
    for _ in range(100):
        if any(h.kind.name == "AUDIO" for h in frames):
            break
        await asyncio.sleep(0.02)
    au = [h.audio_ext for h in frames if h.kind.name == "AUDIO"]
    assert au and (au[-1].pass_lo_hz, au[-1].pass_hi_hz) == (-3000, 3000)
    # AM's channel is 12 kS/s: +-5.7 kHz usable. +-9 kHz fits the capture span but not the channel.
    ack, res = await c.outcome(await c.send("channel.open", receiver=RX, frequency_hz=10_000_000, mode="am",
                                            passband_hz=[-9000, 9000]))
    assert res.status == "failed" and res.code is ErrorCode.E_OUT_OF_RANGE
    await core.shutdown()
