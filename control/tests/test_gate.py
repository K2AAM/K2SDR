"""Generation gate and decoder event parsing (A7 unit level)."""

from __future__ import annotations

import json

import pytest

from k2websdr.decoders.gate import GenerationGate
from k2websdr.protocol.decoder import DecoderEventError, parse_event_line


def line(**over) -> str:
    ev = {"v": 1, "receiver": "rx", "capture_gen": 5, "channel": "ch_a", "channel_gen": 2,
          "sample_index": "123", "utc": "2026-10-26T18:00:00Z", "mode": "ft8", "raw": "CQ K2AAM FN33",
          "engine": {"name": "wsjtx", "version": "2.7.0"}}
    ev.update(over)
    return json.dumps(ev)


@pytest.fixture
def gate() -> GenerationGate:
    g = GenerationGate()
    g.set_capture_gen("rx", 5)
    g.set_channel_gen("ch_a", "rx", 2)
    return g


def test_current_event_passes(gate):
    assert gate.admit(parse_event_line(line()))
    assert gate.stats.passed == 1 and gate.stats.dropped_total == 0


@pytest.mark.parametrize("over,reason", [
    ({"capture_gen": 4}, "stale_capture_gen"),
    ({"capture_gen": 6}, "future_capture_gen"),
    ({"channel_gen": 1}, "stale_channel_gen"),
    ({"channel_gen": 3}, "future_channel_gen"),
    ({"channel": "ch_b"}, "unknown_channel"),
    ({"receiver": "other"}, "unknown_receiver"),
])
def test_mismatched_events_are_dropped_and_counted(gate, over, reason):
    assert not gate.admit(parse_event_line(line(**over)))
    assert gate.stats.dropped == {reason: 1}


def test_channel_on_other_receiver_is_dropped(gate):
    gate.set_capture_gen("rx2", 5)
    assert not gate.admit(parse_event_line(line(receiver="rx2")))
    assert gate.stats.dropped["channel_receiver_mismatch"] == 1


def test_generations_cannot_go_backwards(gate):
    with pytest.raises(ValueError):
        gate.set_capture_gen("rx", 4)
    with pytest.raises(ValueError):
        gate.set_channel_gen("ch_a", "rx", 1)


def test_retune_turns_in_flight_events_stale(gate):
    ev = parse_event_line(line())
    gate.set_capture_gen("rx", 6)
    assert not gate.admit(ev)


def test_removing_a_channel_drops_its_events(gate):
    gate.remove_channel("ch_a")
    assert not gate.admit(parse_event_line(line()))
    gate.remove_receiver("rx")
    assert gate.capture_gen("rx") is None


def test_big_sample_index_is_kept_exact():
    ev = parse_event_line(line(sample_index=str(2**63)))
    assert int(ev.sample_index) == 2**63


@pytest.mark.parametrize("bad", [
    "not json", "[]", line(capture_gen=-1), line(capture_gen=1.0), line(sample_index=123),
    line(sample_index="01"), line(utc="2026-10-26T18:00:00"), line(extra=1), line(raw="x" * 5000),
    line(confidence=1.5), line(v=2), "{" * 20_000,
])
def test_bad_event_lines_are_rejected(bad):
    with pytest.raises(DecoderEventError):
        parse_event_line(bad)


def test_decoded_text_is_kept_verbatim_as_data():
    # Untrusted radio text is stored as-is; escaping happens at display time.
    ev = parse_event_line(line(raw='<script>alert(1)</script> "CQ"'))
    assert ev.raw == '<script>alert(1)</script> "CQ"'
