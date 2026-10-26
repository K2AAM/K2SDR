"""Human frequency input -> integer Hz.

This is the ONLY place unit strings are accepted. The wire protocol carries
integer *_hz fields exclusively; this helper serves explicit UI/CLI input
paths and is never applied to protocol messages.

Parsing is exact decimal-string arithmetic. No float is ever constructed, so
"7.074 MHz" is exactly 7074000, never 7073999.999...

Shared test vectors: protocol/vectors/frequency_input.json (the TypeScript
parser in M1 must pass the same file).
"""

from __future__ import annotations

import re
from enum import Enum

# Outline §2/§3: explicit Hz / kHz / MHz. No other units.
_SCALE = {"hz": 1, "khz": 1_000, "mhz": 1_000_000}

# Digits, optional single '.' followed by at least one digit, then optional
# spaces/tabs, then an optional unit. No signs, exponents, separators or
# leading/trailing '.'.
_RE = re.compile(r"^(?P<int>[0-9]+)(?:\.(?P<frac>[0-9]+))?[ \t]*(?P<unit>[A-Za-z]+)?\Z")

MAX_HZ = 10_000_000_000  # matches the protocol's global Hz bound; device ranges are narrower
MAX_INPUT_LEN = 32       # bounds int() work on hostile input


class FreqErrorCode(str, Enum):
    E_EMPTY = "E_EMPTY"
    E_SYNTAX = "E_SYNTAX"
    E_UNIT_REQUIRED = "E_UNIT_REQUIRED"
    E_UNKNOWN_UNIT = "E_UNKNOWN_UNIT"
    E_AMBIGUOUS_UNIT = "E_AMBIGUOUS_UNIT"
    E_SUB_HZ = "E_SUB_HZ"
    E_OUT_OF_RANGE = "E_OUT_OF_RANGE"


class FrequencyParseError(ValueError):
    def __init__(self, code: FreqErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def _unit_scale(unit: str) -> int:
    # "mHz" is the SI spelling of millihertz. Accepting it as MHz would be a
    # silent 10^9 unit mistake (outline §13), so it is refused outright.
    if unit == "mHz":
        raise FrequencyParseError(FreqErrorCode.E_AMBIGUOUS_UNIT,
                                  "'mHz' means millihertz; write MHz for megahertz")
    scale = _SCALE.get(unit.lower())
    if scale is None:
        raise FrequencyParseError(FreqErrorCode.E_UNKNOWN_UNIT,
                                  f"unknown unit {unit!r}; use Hz, kHz or MHz")
    return scale


def parse_frequency(text: str, *, default_unit: str | None = None, max_hz: int = MAX_HZ) -> int:
    """Parse human input such as '7.074 MHz' or '7074 kHz' into integer Hz.

    A bare number is rejected unless the caller passes default_unit, which a UI
    must display alongside the input so the interpretation is visible.
    Values that do not resolve to a whole number of Hz are rejected, not rounded.
    """
    s = text.strip()
    if not s:
        raise FrequencyParseError(FreqErrorCode.E_EMPTY, "empty frequency")
    if len(s) > MAX_INPUT_LEN:
        raise FrequencyParseError(FreqErrorCode.E_SYNTAX, "frequency input too long")
    m = _RE.match(s)
    if m is None:
        raise FrequencyParseError(FreqErrorCode.E_SYNTAX,
                                  f"cannot read {text!r}; expected e.g. '7.074 MHz' (no commas or signs)")
    unit = m.group("unit")
    if unit is None:
        if default_unit is None:
            raise FrequencyParseError(FreqErrorCode.E_UNIT_REQUIRED,
                                      f"{text!r} has no unit; add Hz, kHz or MHz")
        unit = default_unit
    scale = _unit_scale(unit)

    int_part = int(m.group("int"))
    frac = m.group("frac") or ""
    # scale is 10**k; fractional digits beyond k must all be zero.
    k = len(str(scale)) - 1
    significant, excess = frac[:k], frac[k:]
    if excess.strip("0"):
        raise FrequencyParseError(FreqErrorCode.E_SUB_HZ,
                                  f"{text!r} is not a whole number of Hz")
    hz = int_part * scale + (int(significant.ljust(k, "0")) if k else 0)
    if hz > max_hz:
        raise FrequencyParseError(FreqErrorCode.E_OUT_OF_RANGE,
                                  f"{hz} Hz exceeds the {max_hz} Hz limit")
    return hz
