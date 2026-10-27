"""Browser stream v1 end to end: the real worker replays the WWV clip behind the real app, and a
WebSocket client receives JSON control messages and binary spectrum/audio on the same socket.

Checks the B6 invariant end to end (after channel.tune bumps channel_gen, no audio of the old
generation follows the result). The real worker happens to deliver frames in order, so this run
cannot force a late old-generation frame; the server's drop of those is tested deterministically
in test_core_stream.py (and fails there if the guard is removed).
"""

from __future__ import annotations

import json
from dataclasses import replace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from k2websdr.config import ReceiverEntry
from k2websdr.protocol import stream as sv
from k2websdr.web.app import create_app

from test_web import ADMIN_PW, BASE, ROOT, do_setup, login, make_cfg, ws_cmd, ws_connect

RX = "wwv"
WWV = ROOT / "fixtures/real/airspyhf/airspyhf_wwv10_48k.sigmf-meta"


@pytest.fixture
def app_client(tmp_path):
    cfg = replace(make_cfg(tmp_path), receivers=(ReceiverEntry(RX, "WWV replay", WWV),), spectrum_fft=1024,
                  spectrum_fps=10)
    with TestClient(create_app(cfg), base_url=BASE, follow_redirects=False) as c:   # real worker
        do_setup(c, cfg)
        login(c, "adam", ADMIN_PW)
        yield c


class Reader:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.text: list[dict] = []
        self.binary: list[sv.Message] = []

    def step(self) -> None:
        msg = self.ws.receive()
        if msg.get("bytes") is not None:
            self.binary.append(sv.unpack(msg["bytes"]))
        else:
            self.text.append(json.loads(msg["text"]))

    def until_result(self, cid: str, limit=2000) -> dict:
        for _ in range(limit):
            self.step()
            r = self.text[-1] if self.text else None
            if r and r.get("type") == "result" and r["id"] == cid:
                return r
        raise AssertionError(f"no result for {cid}")

    def until(self, pred, limit=2000) -> None:
        for _ in range(limit):
            self.step()
            if pred(self):
                return
        raise AssertionError("condition not reached")


def test_spectrum_and_own_audio_on_the_control_socket(app_client):
    with ws_connect(app_client) as ws:
        r = Reader(ws)
        res = r.until_result(ws_cmd(ws, "stream.spectrum", receiver=RX, enabled=True))
        assert res["status"] == "applied"
        opened = r.until_result(ws_cmd(ws, "channel.open", receiver=RX, frequency_hz=10_000_000, mode="am"))
        assert opened["status"] == "applied", opened
        cid, gen1 = opened["channel"], opened["channel_gen"]

        def audio(rd, gen):
            return [m for m in rd.binary if m.kind is sv.Kind.AUDIO and m.gen == gen]

        r.until(lambda rd: len(audio(rd, gen1)) >= 10
                and sum(m.kind is sv.Kind.SPECTRUM for m in rd.binary) >= 5)
        spec = [m for m in r.binary if m.kind is sv.Kind.SPECTRUM]
        assert {(m.id, m.sample_rate_hz, m.count, m.frequency_hz) for m in spec} == {(RX, 48000, 1024, 10_001_000)}
        carrier = 512 + round(-1000 * 1024 / 48000)                       # 10.000 MHz in a 10.001 MHz capture
        assert abs(int(np.argmax(np.mean([np.frombuffer(m.payload, np.uint8) for m in spec], axis=0)))
                   - carrier) <= 1
        au = audio(r, gen1)
        assert {(m.id, m.frequency_hz, m.sample_rate_hz) for m in au} == {(cid, 10_000_000, 12000)}
        assert sv.Flags.NEW_GEN in au[0].flags and au[0].sample_index == 0
        idx = [m.sample_index for m in au]
        assert idx == sorted(idx) and len(set(idx)) == len(idx)
        pcm = np.concatenate([np.frombuffer(m.payload, "<i2") for m in au[2:]]).astype(float)
        assert np.sqrt(np.mean(pcm ** 2)) > 1000                         # WWV audio, AGC'd, not silence

        # A mode change bumps channel_gen: once its result is here, no old-generation audio follows.
        tuned = r.until_result(ws_cmd(ws, "channel.tune", channel=cid, mode="usb"))
        gen2 = tuned["channel_gen"]
        assert tuned["status"] == "applied" and gen2 > gen1
        mark = len(r.binary)
        r.until(lambda rd: len(audio(rd, gen2)) >= 5)
        after = [m for m in r.binary[mark:] if m.kind is sv.Kind.AUDIO]
        assert all(m.gen == gen2 for m in after)
        first = audio(r, gen2)[0]
        assert sv.Flags.NEW_GEN in first.flags and first.sample_index == 0

        # Unsubscribe: spectrum stops; audio continues.
        r.until_result(ws_cmd(ws, "stream.spectrum", receiver=RX, enabled=False))
        mark = len(r.binary)
        r.until(lambda rd: sum(m.kind is sv.Kind.AUDIO for m in rd.binary[mark:]) >= 15)
        assert not any(m.kind is sv.Kind.SPECTRUM for m in r.binary[mark + 2:])   # at most one in flight


def test_a_second_session_gets_no_audio_of_the_first(app_client):
    with ws_connect(app_client) as a, ws_connect(app_client) as b:
        ra, rb = Reader(a), Reader(b)
        opened = ra.until_result(ws_cmd(a, "channel.open", receiver=RX, frequency_hz=10_000_000, mode="am"))
        assert opened["status"] == "applied"
        ra.until(lambda rd: sum(m.kind is sv.Kind.AUDIO for m in rd.binary) >= 5)
        rb.until_result(ws_cmd(b, "session.hello", client="test"))
        rb.until_result(ws_cmd(b, "stream.spectrum", receiver=RX, enabled=True))
        rb.until(lambda rd: sum(m.kind is sv.Kind.SPECTRUM for m in rd.binary) >= 3)
        assert not any(m.kind is sv.Kind.AUDIO for m in rb.binary)


@pytest.mark.parametrize("over", [{"spectrum_fft": 1000}, {"spectrum_fft": 32}, {"spectrum_fft": 131072},
                                  {"spectrum_fps": 0}, {"spectrum_fps": 101}])
def test_spectrum_settings_are_validated(tmp_path, over):
    from k2websdr.config import ConfigError, validate
    with pytest.raises(ConfigError, match="spectrum"):
        validate(replace(make_cfg(tmp_path), **over))


def test_service_receivers_ask_the_worker_for_spectrum_and_no_raw_iq(tmp_path):
    from k2websdr.replay import sigmf
    from k2websdr.replay.worker_argv import WorkerIdentity, replay_argv
    rec = sigmf.load(WWV)
    ident = WorkerIdentity(device_id="x", lock_dir=tmp_path, receiver_idx=0)
    plain = replay_argv("w", rec, ident, ctl_fd=3, data_fd=4)
    assert "--iq-frames" not in plain and "--spectrum-fft" not in plain          # M0 behaviour unchanged
    argv = replay_argv("w", rec, ident, ctl_fd=3, data_fd=4, iq_frames=False, spectrum_fft=2048, spectrum_fps=15)
    assert " ".join(argv).count("--iq-frames off --spectrum-fft 2048 --spectrum-fps 15") == 1
