"""A1 (part): human frequency input -> integer Hz, against the shared vectors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from k2websdr.protocol import freq
from k2websdr.protocol.freq import FrequencyParseError, parse_frequency

VECTORS = json.loads(
    (Path(__file__).resolve().parents[2] / "protocol/vectors/frequency_input.json").read_text("utf-8")
)


def test_vector_file_bound_matches_parser():
    assert VECTORS["max_hz"] == freq.MAX_HZ


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: repr(c["input"]))
def test_vector(case):
    kwargs = {"default_unit": case["default_unit"]} if "default_unit" in case else {}
    if "hz" in case:
        got = parse_frequency(case["input"], **kwargs)
        assert got == case["hz"]
        assert type(got) is int
    else:
        with pytest.raises(FrequencyParseError) as ei:
            parse_frequency(case["input"], **kwargs)
        assert ei.value.code.value == case["error"]


def test_every_case_has_exactly_one_expectation():
    for c in VECTORS["cases"]:
        assert ("hz" in c) != ("error" in c), c


def test_vectors_cover_every_error_code():
    seen = {c["error"] for c in VECTORS["cases"] if "error" in c}
    assert seen == {e.value for e in freq.FreqErrorCode}


def test_no_float_rounding_on_awkward_values():
    # Values whose float representation is inexact; exact decimal parsing must win.
    assert parse_frequency("14.074555 MHz") == 14_074_555
    assert parse_frequency("0.1 kHz") == 100
    assert parse_frequency("7.000001 MHz") == 7_000_001


def test_hostile_inputs_raise_only_parse_errors():
    for s in ["9" * 5000 + " Hz", "\x00", "7\n074 kHz", "７０７４ kHz", "7.074 MHz​"]:
        with pytest.raises(FrequencyParseError):
            parse_frequency(s)
