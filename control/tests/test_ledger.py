"""A9: baseline ledger is complete, frozen, matches its sources, and makes nothing available."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from k2websdr.core.service import ControlCore
from k2websdr.ledger.load import LedgerEntry, availability, load_ledger, summary, unavailable_reasons

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "ledger/modes.v1.json"
OUTLINE = ROOT / "docs/outline/WebSDR_Project_Outline-v0.1.md"


def _tool(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"tools/{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


IMPORT = _tool("import_ledger")
DIFF = _tool("diff_ledger_upstream")


def test_committed_ledger_matches_outline_appendix():
    assert IMPORT.main(["x", "--check", str(OUTLINE), str(LEDGER)]) == 0


def test_ledger_shape():
    led = load_ledger(LEDGER)
    assert len(led.entries) == 74
    assert Counter(e.kind for e in led.entries) == {"mode": 71, "service": 3}
    assert {e.id for e in led.entries if e.kind == "service"} == {"audio", "meteor-lrpt", "elektro-lrit"}
    assert led.provenance.upstream.blob == "ad85d5c7de124f0c125e5b045ada283a97acc1c9"
    assert led.provenance.upstream.commit == "86915038202fb273c13274420c7e882b32b19a0a"


def test_ledger_is_data_only_in_m0():
    led = load_ledger(LEDGER)
    for e in led.entries:
        assert (e.status, e.adapter, e.engine_version, e.test_result, e.live_validation) == \
            ("not_started", None, None, None, "none"), e.id


def test_nothing_is_available_and_every_mode_says_why():
    led = load_ledger(LEDGER)
    reasons = availability(led)
    assert all(reasons[e.id] for e in led.entries)
    assert all("no adapter integrated" in reasons[e.id][0] for e in led.entries)
    assert "required dependency 'ale2g' not detected on this host" in reasons["ale2g"]
    assert summary(led).available == []


def test_core_exposes_empty_availability(tmp_path):
    core = ControlCore(ledger=summary(load_ledger(LEDGER)))
    assert core.store.view()["ledger"] == {"version": 1, "available": []}


def _entry(**kw) -> LedgerEntry:
    base = dict(id="ft8", name="FT8", kind="mode", required_feature="wsjt-x", family=None, status="integrated",
                adapter="wsjtx-jt9", engine_version="2.7.0", supported_inputs=["audio-12k"],
                reference_sample="fixtures/audio/ft8.wav", test_result="pass 2026-10-26", live_validation="none",
                limitations=[])
    base.update(kw)
    return LedgerEntry(**base)


def test_availability_rules():
    assert unavailable_reasons(_entry(), frozenset({"wsjt-x"})) == []
    assert unavailable_reasons(_entry(), frozenset()) == ["required dependency 'wsjt-x' not detected on this host"]
    assert "no adapter integrated" in unavailable_reasons(_entry(status="in_progress"), frozenset({"wsjt-x"}))[0]
    assert unavailable_reasons(_entry(test_result=None), frozenset({"wsjt-x"})) == ["adapter has no recorded test result"]
    assert unavailable_reasons(_entry(required_feature=None), frozenset()) == []


def test_ledger_rejects_unknown_fields_and_duplicates(tmp_path):
    doc = json.loads(LEDGER.read_text())
    doc["entries"][0]["surprise"] = 1
    p = tmp_path / "l.json"
    p.write_text(json.dumps(doc))
    with pytest.raises(Exception):
        load_ledger(p)
    doc = json.loads(LEDGER.read_text())
    doc["entries"].append(doc["entries"][0])
    p.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        load_ledger(p)


# ---------------------------------------------------- upstream diff tool

SYNTHETIC = '''
raise SystemExit("this file must never be executed")

class Modes(object):
    mappings = [
        AnalogMode("nfm", "FM"),
        WsjtMode("ft8", "FT8"),
        DigitalMode("ale2g", "ALE 2G (experimental)", underlying=["usb"],
                    requirements=["ale2g"]),
#       DigitalMode("ghost", "Commented out"),
        ServiceOnlyMode("audio", "Audio Recorder", requirements=["mp3"]),
    ]
'''


def _ledger_for(tmp_path, entries) -> Path:
    doc = json.loads(LEDGER.read_text())
    doc["entries"] = entries
    p = tmp_path / "led.json"
    p.write_text(json.dumps(doc))
    return p


def _row(i, name, kind="mode", feat=None):
    return {"id": i, "name": name, "kind": kind, "required_feature": feat}


def _report(tmp_path, entries, src=SYNTHETIC) -> str:
    up = tmp_path / "modes.py"
    up.write_text(src)
    buf = io.StringIO()
    with redirect_stdout(buf):
        DIFF.main(["x", str(up), str(_ledger_for(tmp_path, entries))])
    return buf.getvalue()


def test_diff_tool_parses_without_executing_and_ignores_comments(tmp_path):
    entries = [_row("nfm", "FM"), _row("ft8", "FT8", feat="wsjt-x"),
               _row("ale2g", "ALE 2G (experimental)", feat="ale2g"),
               _row("audio", "Audio Recorder", kind="service", feat="mp3")]
    out = _report(tmp_path, entries)
    assert "Active entries in `Modes.mappings`: 4" in out
    assert "Field differences: 0" in out and "Same order: yes" in out


def test_diff_tool_reports_every_kind_of_discrepancy(tmp_path):
    entries = [_row("nfm", "NFM"),                                   # renamed
               _row("ale2g", "ALE 2G (experimental)", feat="ale3g"),  # requirement changed, order changed
               _row("audio", "Audio Recorder", kind="mode", feat="mp3"),  # kind changed
               _row("extra", "Extra")]                              # not upstream
    out = _report(tmp_path, entries)
    assert "In upstream but not ledger: ft8" in out
    assert "In ledger but not upstream: extra" in out
    assert "Same order: no" in out
    assert "`nfm` name" in out and "`ale2g` requirement" in out and "`audio` kind" in out
    assert "Field differences: 3" in out
