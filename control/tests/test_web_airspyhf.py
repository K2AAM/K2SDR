"""A live Airspy HF+ receiver end to end, without hardware: the real app, configured with a
driver = "airspyhf" receiver, runs ksdr-worker-airspyhf-mock (the worker linked against the mock
libairspyhf) as its worker. Over /ws: spectrum and audio, receiver.set_hw_gain, a retune that
keeps the channel, then an unplug: the worker exits without stop/close, the receiver shows the
loss, restarts by itself where the device was (centre, rate, gain) and the channel plays again.
"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from k2websdr.config import AirspyhfEntry, ConfigError, ReceiverEntry, load
from k2websdr.protocol import stream as sv
from k2websdr.web.app import create_app

from test_web import ADMIN_PW, BASE, ROOT, do_setup, login, make_cfg, ws_cmd, ws_connect
from test_web_stream import Reader

MOCK_BIN = Path(os.environ.get("K2_WORKER_AIRSPYHF_MOCK_BIN", ROOT / "build/worker/ksdr-worker-airspyhf-mock"))
RX = "hfplus"
SERIAL = 0xA1B2C3D4E5F60718

if not MOCK_BIN.exists():
    if os.environ.get("K2_REQUIRE_PROBE") == "1":
        raise RuntimeError(f"{MOCK_BIN} missing; build with libairspyhf-dev installed")
    pytest.skip("airspyhf mock worker not built", allow_module_level=True)


def call_log(path: Path) -> list[tuple[str, int]]:
    return [(c, int(v)) for c, v in (ln.split() for ln in path.read_text().splitlines())] if path.exists() else []


@pytest.fixture
def live_app(tmp_path, monkeypatch):
    calls = tmp_path / "calls.log"
    # The worker inherits the service's environment: these script the mock device.
    for k, v in {"MOCK_CALL_LOG": str(calls), "MOCK_SETTLE_MS": "0", "MOCK_UNPLUG_AT_S": "2.0",
                 "MOCK_UNPLUG_ONCE_FILE": str(tmp_path / "unplugged"), "MOCK_STOP_HANG": "1"}.items():
        monkeypatch.setenv(k, v)
    entry = AirspyhfEntry(serial=SERIAL, center_hz=10_000_000, rate_hz=384000, freq_range_hz=(10_000, 30_000_000))
    cfg = replace(make_cfg(tmp_path), worker_bin=MOCK_BIN, spectrum_fft=1024, spectrum_fps=10,
                  receivers=(ReceiverEntry(RX, "Airspy HF+", airspyhf=entry),))
    with TestClient(create_app(cfg), base_url=BASE, follow_redirects=False) as c:
        do_setup(c, cfg)
        login(c, "adam", ADMIN_PW)
        yield c, calls


def receiver(c) -> dict:
    return c.get("/api/state").json()["state"]["receivers"][RX]


def wait_for(pred, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("condition not reached")


def test_live_receiver_end_to_end_with_unplug_and_recovery(live_app):
    c, calls = live_app
    st = receiver(c)
    assert (st["driver"], st["acquisition"]["state"]) == ("airspyhf", "running")
    assert st["capture"]["hw_gain"]["applied"] == {"hf_agc": True, "hf_agc_threshold": "low", "hf_lna": True,
                                                    "hf_att": 0}
    with ws_connect(c) as ws:
        r = Reader(ws)
        assert r.until_result(ws_cmd(ws, "stream.spectrum", receiver=RX, enabled=True))["status"] == "applied"
        opened = r.until_result(ws_cmd(ws, "channel.open", receiver=RX, frequency_hz=10_004_000, mode="usb"))
        assert opened["status"] == "applied", opened
        cid = opened["channel"]
        r.until(lambda rd: sum(m.kind is sv.Kind.AUDIO for m in rd.binary) >= 3)

        assert r.until_result(ws_cmd(ws, "receiver.claim", receiver=RX))["status"] == "applied"
        res = r.until_result(ws_cmd(ws, "receiver.set_hw_gain", receiver=RX, hf_agc=False, hf_att=4))
        assert res["status"] == "applied", res
        res = r.until_result(ws_cmd(ws, "receiver.set_capture", receiver=RX, center_hz=10_050_000))
        assert res["status"] == "applied" and res["capture_gen"] == 2

        # The mock pulls the cable 2 s into the first worker's stream.
        wait_for(lambda: receiver(c)["acquisition"]["state"] == "failed")
        assert receiver(c)["acquisition"]["detail"].startswith("device not found or lost; retrying in")
        old_gen = c.get("/api/state").json()["state"]["channels"][cid]["channel_gen"]
        back = wait_for(lambda: (s := receiver(c))["acquisition"]["state"] == "running" and s["capture_gen"] > 2
                        and s)
        assert back["capture"]["center_hz"]["applied"] == 10_050_000
        assert back["capture"]["hw_gain"]["applied"]["hf_att"] == 4
        assert cid in c.get("/api/state").json()["state"]["channels"]              # reopened, not lost
        new_gen = wait_for(lambda: (g := c.get("/api/state").json()["state"]["channels"][cid]["channel_gen"]) != old_gen
                           and g)
        # The new worker numbers channels from 1 again; browsers drop audio of an older channel_gen,
        # so the published one must still increase (Window 3: no audio after replug until reload).
        assert new_gen > old_gen
        r.until(lambda rd: sum(m.kind is sv.Kind.AUDIO and m.gen == new_gen for m in rd.binary) >= 3)
        assert {m.frequency_hz for m in r.binary if m.kind is sv.Kind.AUDIO and m.gen == new_gen} == {10_004_000}

    log = call_log(calls)
    starts = [i for i, x in enumerate(log) if x[0] == "set_samplerate"]
    assert len(starts) >= 2
    first_run, second_open = log[:starts[1]], log[starts[1]:starts[1] + 8]
    assert ("stop", 0) not in first_run and ("close", 0) not in first_run   # loss path: never stop/close
    assert second_open == [("set_samplerate", 384000), ("set_freq", 10_050_000), ("set_lib_dsp", 1),
                           ("set_hf_agc", 0), ("set_hf_agc_threshold", 0), ("set_hf_lna", 1),
                           ("set_hf_att", 4), ("start", 0)]


# --------------------------------------------------------------------------------------- config

def write(tmp_path, body: str) -> Path:
    p = tmp_path / "k2.toml"
    p.write_text('[server]\nstate_dir = "s"\nruntime_dir = "r"\n' + body)
    return p


GOOD = '''
[[receivers]]
id = "hfplus"
label = "Airspy HF+ Discovery"
driver = "airspyhf"
serial = "A1B2C3D4E5F60718"
center_hz = 7100000
rate_hz = 384000
freq_range_hz = [10000, 30000000]
'''


def test_config_reads_an_airspyhf_receiver(tmp_path):
    cfg = load(write(tmp_path, GOOD + 'hf_att = 2\nhf_agc = false\n'))
    e = cfg.receivers[0].airspyhf
    assert (e.serial, e.center_hz, e.rate_hz, e.freq_range_hz, e.hf_att, e.hf_agc, e.hf_lna) == (
        SERIAL, 7_100_000, 384000, (10_000, 30_000_000), 2, False, True)


@pytest.mark.parametrize("extra, message", [
    ('center_hz = 7100000.0\n', "integer Hz"),
    ('hf_att = 9\n', "hf_att"),
    ('hf_att = true\n', "hf_att"),
    ('hf_agc_threshold = "mid"\n', "hf_agc_threshold"),
    ('rate_hz2 = 1\n', "unknown keys"),
    ('sample_rates_hz = [384000, 999999]\n', "subset"),
])
def test_config_rejects_bad_airspyhf_receivers(tmp_path, extra, message):
    body = GOOD
    if extra.startswith("center_hz"):
        body = body.replace("center_hz = 7100000\n", extra)
    else:
        body += extra
    with pytest.raises(ConfigError, match=message):
        load(write(tmp_path, body))


def test_config_rejects_out_of_range_centre_missing_serial_and_duplicates(tmp_path):
    with pytest.raises(ConfigError, match="outside"):
        load(write(tmp_path, GOOD.replace("center_hz = 7100000", "center_hz = 5000")))
    with pytest.raises(ConfigError, match="missing serial"):
        load(write(tmp_path, GOOD.replace('serial = "A1B2C3D4E5F60718"\n', "")))
    with pytest.raises(ConfigError, match="same Airspy"):
        load(write(tmp_path, GOOD + GOOD.replace('id = "hfplus"', 'id = "other"')))
    with pytest.raises(ConfigError, match="unknown driver"):
        load(write(tmp_path, GOOD.replace('"airspyhf"', '"rtlsdr"')))
