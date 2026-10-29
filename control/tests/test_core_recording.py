"""Server-side recordings (M1 step 6, design §3.6): a server-owned channel per recording, WAV of the
clean 12 kS/s audio, filename <UTC>_<frequency_hz>_<mode>.wav, bounded storage, catalog in SQLite."""

from __future__ import annotations

import re
import sqlite3
import wave
from collections import deque
from pathlib import Path

from k2websdr.core.recording import RecordingCatalog, StoragePolicy
from k2websdr.core.service import ControlCore, Principal
from k2websdr.db.migrate import migrate
from k2websdr.protocol import frame as fr
from k2websdr.protocol import models as m
from k2websdr.protocol.errors import ErrorCode

from core_helpers import CENTER, LISTENER, RATE, Client, async_test, rx_config
from fake_worker import FakeWorker, fake_factory
from test_core_stream import audio_frame

RX = "replay-0"
M1 = ["am", "usb", "lsb", "cw", "nfm", "wfm"]
OPERATOR = Principal("user:adam", frozenset({"listen", "hw.retune", "hw.antenna", "record"}))   # role operator
OTHER = Principal("user:bob", frozenset({"listen", "record"}))
ADMIN = Principal("user:root", frozenset({"listen", "record", "admin"}))


def catalog(tmp: Path, cap=10**9, min_free=0) -> RecordingCatalog:
    conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return RecordingCatalog(conn, StoragePolicy(tmp / "rec", cap, min_free))


async def make(tmp, script=(), **kw):
    FakeWorker.instances.clear()
    cat = catalog(tmp, **kw)
    core = ControlCore(ledger=m.LedgerSummary(version=1, available=M1), recordings=cat)
    rt = core.add_receiver(rx_config(RX, restart_backoff=0.01), fake_factory(deque(script)))
    await core.start()
    return core, rt, cat


async def start(c: Client, freq=CENTER + 1000, mode="usb"):
    ack, res = await c.outcome(await c.send("recording.start", receiver=RX, frequency_hz=freq, mode=mode))
    return ack, res


def feed(rt, res, n=3, *, gen=None, flags=fr.FrameFlags(0)):
    wid = int(res.channel[3:])
    for i in range(n):
        rt.on_frame(*audio_frame(rt, wid, res.channel_gen if gen is None else gen, index=1200 * i,
                                 flags=flags if i == 1 else fr.FrameFlags(0)))


@async_test
async def test_record_permission_is_required(tmp_path):
    core, _, _ = await make(tmp_path)
    ack, _ = await start(Client(core, LISTENER))
    assert ack.code is ErrorCode.E_FORBIDDEN
    await core.shutdown()


@async_test
async def test_a_recording_writes_a_valid_wav_named_and_catalogued_with_the_same_integer_hz(tmp_path):
    core, rt, cat = await make(tmp_path)
    c = Client(core, OPERATOR)
    await c.subscribe()
    ack, res = await start(c, CENTER + 1500, "usb")
    assert res.status == "applied" and res.recording and res.channel, (ack, res)
    st = c.mirror["recordings"][res.recording]
    assert (st["frequency_hz"], st["mode"], st["owner"], st["channel"]) == (CENTER + 1500, "usb", "user:adam", res.channel)
    assert c.mirror["channels"][res.channel]["owner"] == {"session": None, "job": f"job_{res.recording.replace('_', '-')}"}
    assert re.fullmatch(rf"\d{{8}}T\d{{6}}Z_{CENTER + 1500}_usb\.wav", st["file"])
    feed(rt, res, 3)
    _, stop = await c.outcome(await c.send("recording.stop", recording=res.recording))
    assert stop.status == "applied" and res.recording not in c.mirror["recordings"]
    assert res.channel not in c.mirror["channels"]                                  # its channel is closed
    row = cat.get(res.recording)
    assert (row["status"], row["frequency_hz"], row["mode"], row["samples"], row["file"]) == (
        "done", CENTER + 1500, "usb", 3600, st["file"])
    with wave.open(str(cat.path_of(row))) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()) == (12000, 1, 2, 3600)
    assert row["bytes"] == 44 + 7200
    await core.shutdown()


@async_test
async def test_recording_survives_the_browser_session_closing(tmp_path):
    core, rt, cat = await make(tmp_path)
    c = Client(core, OPERATOR)
    _, res = await start(c)
    core.close_session(c.session)                       # browser suspended / tab closed
    feed(rt, res, 2)
    c2 = Client(core, OPERATOR)                         # back on another device, same user
    await c2.subscribe()
    assert res.recording in c2.mirror["recordings"]
    _, stop = await c2.outcome(await c2.send("recording.stop", recording=res.recording))
    assert stop.status == "applied" and cat.get(res.recording)["samples"] == 2400
    await core.shutdown()


@async_test
async def test_stale_generation_audio_is_not_recorded_and_gaps_are_counted(tmp_path):
    core, rt, cat = await make(tmp_path)
    c = Client(core, OPERATOR)
    _, res = await start(c)
    feed(rt, res, 2, gen=res.channel_gen + 5)            # not the channel's generation
    feed(rt, res, 3, flags=fr.FrameFlags.DISCONTINUITY)  # one gap inside
    await c.outcome(await c.send("recording.stop", recording=res.recording))
    row = cat.get(res.recording)
    assert (row["samples"], row["gaps"]) == (3600, 1)
    await core.shutdown()


@async_test
async def test_only_the_owner_or_an_admin_stops_a_recording(tmp_path):
    core, _, _ = await make(tmp_path)
    a, b, root = Client(core, OPERATOR), Client(core, OTHER), Client(core, ADMIN)
    _, res = await start(a)
    ack, _ = await b.outcome(await b.send("recording.stop", recording=res.recording))
    assert ack.code is ErrorCode.E_FORBIDDEN
    ack, _ = await b.outcome(await b.send("recording.stop", recording="rec_999"))
    assert ack.code is ErrorCode.E_UNKNOWN_RECORDING
    _, r = await root.outcome(await root.send("recording.stop", recording=res.recording))
    assert r.status == "applied"
    await core.shutdown()


@async_test
async def test_storage_cap_refuses_new_recordings_and_is_visible(tmp_path):
    core, _, cat = await make(tmp_path, cap=0)
    c = Client(core, OPERATOR)
    await c.subscribe()
    ack, _ = await start(c)
    assert ack.code is ErrorCode.E_STORAGE_FULL
    c.drain()
    assert c.mirror["storage"]["full"] is True and "cap" in c.mirror["storage"]["detail"]
    await core.shutdown()


@async_test
async def test_reaching_the_cap_while_recording_ends_it_with_a_valid_file(tmp_path, monkeypatch):
    import k2websdr.core.service as svc
    monkeypatch.setattr(svc, "RECORDING_STATE_INTERVAL_S", 0.0)
    core, rt, cat = await make(tmp_path, cap=44 + 2400 * 2 + 10)
    c = Client(core, OPERATOR)
    await c.subscribe()
    _, res = await start(c)
    feed(rt, res, 3)                                     # the third frame crosses the cap
    row = cat.get(res.recording)
    assert row["status"] == "storage_full" and res.recording not in core.store.view()["recordings"]
    with wave.open(str(cat.path_of(row))) as w:
        assert w.getnframes() == row["samples"] >= 2400
    await core.shutdown()


@async_test
async def test_receiver_retune_away_interrupts_the_recording(tmp_path):
    # The device ends up 10 spans away (fake readback): the recorded frequency is no longer covered.
    core, rt, cat = await make(tmp_path, script=[("apply", CENTER + 10 * RATE)])
    op = Client(core, OPERATOR)
    await op.subscribe()
    _, res = await start(op, CENTER + 1000)
    await op.outcome(await op.send("receiver.claim", receiver=RX))
    _, r = await op.outcome(await op.send("receiver.set_capture", receiver=RX, center_hz=CENTER))
    assert r.status == "applied"
    for _ in range(50):
        if cat.get(res.recording)["status"] != "recording":
            break
        await __import__("asyncio").sleep(0.01)
    assert cat.get(res.recording)["status"] == "interrupted"
    await core.shutdown()


@async_test
async def test_a_retune_that_would_end_a_recording_asks_first(tmp_path):
    # Adam, 2026-10-28: warn that it is still recording, with an option to end it.
    away = CENTER + 10 * RATE
    core, rt, cat = await make(tmp_path, script=[("apply", CENTER + 2000), ("apply", away)])
    with core.store.txn() as t:          # a replay centre is fixed; widen it as a tunable device's
        t.replace("receivers", RX, "capabilities", "freq_range_hz", value=[CENTER - RATE, away + RATE])
    op = Client(core, OPERATOR)
    await op.subscribe()
    _, res = await start(op, CENTER + 1000)
    await op.outcome(await op.send("receiver.claim", receiver=RX))
    ack, _ = await op.outcome(await op.send("receiver.set_capture", receiver=RX, center_hz=away))
    assert ack.status == "rejected" and ack.code == "E_RECORDING_ACTIVE", ack
    assert res.recording in ack.detail and str(CENTER + 1000) in ack.detail
    assert cat.get(res.recording)["status"] == "recording"                 # untouched
    # A retune the recording survives needs no confirmation.
    ack, r = await op.outcome(await op.send("receiver.set_capture", receiver=RX, center_hz=CENTER + 2000))
    assert ack.status == "accepted", ack
    _, r = await op.outcome(await op.send("receiver.set_capture", receiver=RX, center_hz=away, end_recordings=True))
    assert r.status == "applied"
    for _ in range(50):
        if cat.get(res.recording)["status"] != "recording":
            break
        await __import__("asyncio").sleep(0.01)
    assert cat.get(res.recording)["status"] == "interrupted"
    await core.shutdown()


@async_test
async def test_shutdown_and_restart_leave_no_recording_marked_running(tmp_path):
    core, rt, cat = await make(tmp_path)
    _, res = await start(Client(core, OPERATOR))
    await core.shutdown()
    assert cat.get(res.recording)["status"] == "interrupted"
    cat.conn.execute("UPDATE recordings SET status='recording' WHERE id=?", (res.recording,))   # crash
    ControlCore(recordings=cat)
    assert cat.get(res.recording)["status"] == "interrupted"


def test_recording_args_are_validated():
    from pydantic import ValidationError
    import pytest
    for bad in ({"frequency_hz": 7.0e6}, {"passband_hz": [2700, 300]}, {"agc": "x"}):
        with pytest.raises(ValidationError):
            m.RecordingStartArgs(**{"receiver": RX, "frequency_hz": 7_000_000, "mode": "usb", **bad})
