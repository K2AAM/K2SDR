"""Recording end to end (M1 step 6): the real worker replays the WWV clip behind the real app; a
recording started over /ws produces a WAV that downloads over authenticated HTTP. B5: the same
integer Hz in the state, the filename and the catalog. B9: correct WAV, access control, deletion."""

from __future__ import annotations

import io
import re
import time
import wave

import numpy as np
import pytest

from test_web import ADMIN_PW, add_user, client, csrf, login, ws_cmd, ws_connect
from test_web_stream import Reader, app_client  # noqa: F401  (fixture)

RX = "wwv"


def test_record_download_and_delete(app_client):
    c = app_client
    with ws_connect(c) as ws:
        r = Reader(ws)
        res = r.until_result(ws_cmd(ws, "recording.start", receiver=RX, frequency_hz=10_000_000, mode="am"))
        assert res["status"] == "applied", res
        rid = res["recording"]
        time.sleep(2.5)                                   # the server runs on its own thread
        state = c.get("/api/state").json()["state"]
        assert state["recordings"][rid]["frequency_hz"] == 10_000_000 and state["storage"]["full"] is False
        assert r.until_result(ws_cmd(ws, "recording.stop", recording=rid))["status"] == "applied"
    rows = c.get("/api/recordings").json()["recordings"]
    row = next(x for x in rows if x["id"] == rid)
    assert row["status"] == "done" and row["frequency_hz"] == 10_000_000 and row["mode"] == "am"
    assert re.fullmatch(r"\d{8}T\d{6}Z_10000000_am\.wav", row["file"])
    dl = c.get(f"/api/recordings/{rid}/file")
    assert dl.status_code == 200 and dl.headers["content-type"] == "audio/wav"
    assert row["file"] in dl.headers["content-disposition"]
    with wave.open(io.BytesIO(dl.content)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (12000, 1, 2)
        n = w.getnframes()
        pcm = np.frombuffer(w.readframes(n), "<i2").astype(float)
    assert n == row["samples"] and 1.5 * 12000 < n < 3.5 * 12000              # about the 2.5 s recorded
    assert np.sqrt(np.mean(pcm[3000:] ** 2)) > 1000                           # AM audio, not silence
    assert c.delete(f"/api/recordings/{rid}").status_code == 403              # no CSRF token
    tok = csrf(c)
    assert c.delete(f"/api/recordings/{rid}", headers={"x-csrf-token": tok, "Origin": "http://127.0.0.1:8075"}
                    ).json() == {"deleted": rid}
    assert c.get(f"/api/recordings/{rid}/file").status_code == 404
    assert all(x["id"] != rid for x in c.get("/api/recordings").json()["recordings"])


def test_recordings_need_record_and_are_private(app_client):
    c = app_client
    with ws_connect(c) as ws:
        r = Reader(ws)
        rid = r.until_result(ws_cmd(ws, "recording.start", receiver=RX, frequency_hz=10_000_000, mode="am"))["recording"]
        r.until_result(ws_cmd(ws, "recording.stop", recording=rid))
    add_user(c, "lis", "listener")
    add_user(c, "op", "operator")
    for user, list_code, file_code in (("lis", 403, 403), ("op", 200, 404)):
        c.cookies.clear()
        login(c, user, "another long password")
        assert c.get("/api/recordings").status_code == list_code
        if list_code == 200:
            assert c.get("/api/recordings").json()["recordings"] == []         # not adam's
        assert c.get(f"/api/recordings/{rid}/file").status_code == file_code
