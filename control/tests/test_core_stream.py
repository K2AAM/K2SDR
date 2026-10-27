"""Browser stream v1 in the control core: spectrum subscriptions, a session's own audio, stale
generations, backpressure (M1 step 3b-2).

Worker frames are synthesized and fed through the receiver's frame callback, exactly where the
worker client delivers them; channels come from the fake worker (same semantics as the real one).
"""

from __future__ import annotations

from k2websdr.core.service import STREAM_AUDIO_QUEUE_BYTES, Principal
from k2websdr.protocol import frame as fr
from k2websdr.protocol import stream as sv
from k2websdr.protocol import worker_ctl as wc
from k2websdr.protocol.errors import ErrorCode

from core_helpers import CENTER, LISTENER, RATE, Client, async_test
from test_core_channels import RX, make, open_channel

F = fr.FrameFlags


def spectrum_frame(rt, *, gen=None, bins=64, flags=F(0), end=4800, fill=7):
    return fr.FrameHeader(kind=fr.FrameKind.SPECTRUM, format=fr.SampleFormat.U8DB, flags=flags,
                          capture_gen=rt.capture_gen if gen is None else gen, seq=0, first_sample_index=end,
                          freq_origin_hz=CENTER, host_time_ns=0, gap_samples=0, receiver_idx=0, stream_id=1,
                          sample_rate_hz=RATE, sample_count=bins, header_len=fr.SPECTRUM_HEADER_LEN,
                          ext=fr.SpectrumExt(-127500, 500, 3).pack()), bytes([fill]) * bins


def audio_frame(rt, worker_id, channel_gen, *, gen=None, n=1200, index=0, flags=F(0), freq=CENTER + 1000):
    return fr.FrameHeader(kind=fr.FrameKind.AUDIO, format=fr.SampleFormat.RI16, flags=flags,
                          capture_gen=rt.capture_gen if gen is None else gen, seq=0, first_sample_index=index,
                          freq_origin_hz=freq, host_time_ns=0, gap_samples=0, receiver_idx=0,
                          stream_id=worker_id, sample_rate_hz=12000, sample_count=n,
                          header_len=fr.AUDIO_HEADER_LEN,
                          ext=fr.AudioExt(channel_gen, wc.Mode.USB, wc.Agc.MEDIUM, -2000, 300, 2700).pack()), \
        bytes(2 * n)


def binary(c: Client) -> list[sv.Message]:
    out = []
    while (b := c.session.next_binary()) is not None:
        out.append(sv.unpack(b))
    return out


async def subscribe_spectrum(c: Client, enabled=True, receiver=RX):
    return await c.outcome(await c.send("stream.spectrum", receiver=receiver, enabled=enabled))


@async_test
async def test_spectrum_goes_only_to_subscribers_with_its_metadata():
    core, rt = await make()
    a, b = Client(core, LISTENER), Client(core, LISTENER)
    ack, res = await subscribe_spectrum(a)
    assert ack.status == "accepted" and res.status == "applied"
    rt.on_frame(*spectrum_frame(rt, flags=F.NEW_GEN))
    [msg] = binary(a)
    assert binary(b) == []
    assert (msg.kind, msg.id, msg.gen, msg.sample_index, msg.frequency_hz, msg.sample_rate_hz) == (
        sv.Kind.SPECTRUM, RX, rt.capture_gen, 4800, CENTER, RATE)
    assert msg.flags == sv.Flags.NEW_GEN and msg.info == sv.SpectrumInfo(-127500, 500, 3)
    assert msg.payload == bytes([7]) * 64
    await subscribe_spectrum(a, enabled=False)
    rt.on_frame(*spectrum_frame(rt))
    assert binary(a) == []
    await core.shutdown()


@async_test
async def test_stream_spectrum_needs_listen_and_a_known_receiver():
    core, _ = await make()
    nobody = Client(core, Principal("user:none", frozenset()))
    ack, _ = await subscribe_spectrum(nobody)
    assert ack.code is ErrorCode.E_FORBIDDEN
    ack, _ = await subscribe_spectrum(Client(core, LISTENER), receiver="nope")
    assert ack.code is ErrorCode.E_UNKNOWN_RECEIVER
    await core.shutdown()


@async_test
async def test_stale_capture_generation_spectrum_is_never_sent():
    core, rt = await make()
    a = Client(core, LISTENER)
    await subscribe_spectrum(a)
    rt.on_frame(*spectrum_frame(rt, gen=rt.capture_gen - 1))
    assert binary(a) == [] and core.stream_stale["spectrum"] == 1
    await core.shutdown()


@async_test
async def test_audio_goes_only_to_the_owner_with_its_metadata():
    core, rt = await make()
    a, b = Client(core, LISTENER), Client(core, LISTENER)
    res = await open_channel(a, CENTER + 1000, "usb")
    wid = int(res.channel[3:])
    rt.on_frame(*audio_frame(rt, wid, res.channel_gen, flags=F.NEW_GEN | F.DISCONTINUITY, index=0))
    [msg] = binary(a)
    assert binary(b) == []
    assert (msg.kind, msg.id, msg.gen, msg.frequency_hz, msg.sample_rate_hz, msg.count) == (
        sv.Kind.AUDIO, res.channel, res.channel_gen, CENTER + 1000, 12000, 1200)
    assert msg.flags == sv.Flags.NEW_GEN | sv.Flags.DISCONTINUITY
    assert msg.info == sv.AudioInfo(-2000, 300, 2700, wc.Mode.USB, wc.Agc.MEDIUM)
    await core.shutdown()


@async_test
async def test_stale_or_unowned_audio_is_dropped():
    core, rt = await make()
    a = Client(core, LISTENER)
    res = await open_channel(a)
    wid = int(res.channel[3:])
    rt.on_frame(*audio_frame(rt, wid, res.channel_gen + 1))              # a generation it does not have
    rt.on_frame(*audio_frame(rt, wid, res.channel_gen, gen=rt.capture_gen - 1))   # old capture
    rt.on_frame(*audio_frame(rt, wid + 100, res.channel_gen))            # no such channel
    assert binary(a) == [] and core.stream_stale["audio"] == 3
    # After a mode change bumps channel_gen, frames of the old generation are stale.
    _, r2 = await a.outcome(await a.send("channel.tune", channel=res.channel, mode="lsb"))
    assert r2.channel_gen > res.channel_gen
    rt.on_frame(*audio_frame(rt, wid, res.channel_gen))
    rt.on_frame(*audio_frame(rt, wid, r2.channel_gen))
    assert [m.gen for m in binary(a)] == [r2.channel_gen]
    await core.shutdown()


@async_test
async def test_closing_a_channel_discards_its_queued_audio_and_later_frames():
    core, rt = await make()
    a = Client(core, LISTENER)
    res = await open_channel(a)
    wid = int(res.channel[3:])
    rt.on_frame(*audio_frame(rt, wid, res.channel_gen))
    await a.outcome(await a.send("channel.close", channel=res.channel))
    rt.on_frame(*audio_frame(rt, wid, res.channel_gen))
    assert binary(a) == []
    await core.shutdown()


@async_test
async def test_slow_reader_keeps_only_the_newest_spectrum_row_and_flags_the_drop():
    core, rt = await make()
    a = Client(core, LISTENER)
    await subscribe_spectrum(a)
    for fill in (1, 2, 3):
        rt.on_frame(*spectrum_frame(rt, fill=fill))
    [msg] = binary(a)
    assert msg.payload[0] == 3 and sv.Flags.SERVER_DROP in msg.flags
    assert a.session.stream_dropped["spectrum"] == 2
    rt.on_frame(*spectrum_frame(rt, fill=4))
    assert sv.Flags.SERVER_DROP not in binary(a)[0].flags                 # flag is one-shot
    await core.shutdown()


@async_test
async def test_slow_reader_audio_is_bounded_oldest_dropped_and_the_gap_flagged():
    core, rt = await make()
    a = Client(core, LISTENER)
    res = await open_channel(a)
    wid = int(res.channel[3:])
    per = 2 * 1200 + sv.FIXED_LEN + 4
    n = STREAM_AUDIO_QUEUE_BYTES // per + 5
    for i in range(n):
        rt.on_frame(*audio_frame(rt, wid, res.channel_gen, index=1200 * i))
    got = binary(a)
    assert sum(len(sv.pack(sv.Kind.AUDIO, 0, 1, 0, 1, 12000, res.channel, m.info, m.payload)) for m in got) \
        <= STREAM_AUDIO_QUEUE_BYTES
    assert a.session.stream_dropped["audio"] == n - len(got) > 0
    idx = [m.sample_index for m in got]
    assert idx == sorted(idx) and idx[-1] == 1200 * (n - 1)              # newest kept, in order
    assert sv.Flags.SERVER_DROP in got[0].flags and not any(sv.Flags.SERVER_DROP in m.flags for m in got[1:])
    await core.shutdown()


@async_test
async def test_closing_the_session_stops_its_stream():
    core, rt = await make()
    a = Client(core, LISTENER)
    await subscribe_spectrum(a)
    a.drain()
    rt.on_frame(*spectrum_frame(rt))
    assert a.session.has_binary()
    core.close_session(a.session)
    rt.on_frame(*spectrum_frame(rt))
    assert core._spectrum_subs[RX] == set()
    await core.shutdown()
