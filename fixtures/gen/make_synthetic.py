#!/usr/bin/env python3
"""Generate the small synthetic SigMF fixtures committed under fixtures/iq/.

    python3 fixtures/gen/make_synthetic.py --write fixtures/iq
    python3 fixtures/gen/make_synthetic.py --check fixtures/iq

Determinism: standard library only. Tone phase is reduced with exact integer
arithmetic ((f * n) mod fs) before a single math.sin/cos call, and noise comes
from an integer xorshift64* generator with a fixed seed. No numpy, so output
does not depend on a numerical library's SIMD code paths. --check regenerates
in memory and compares byte-for-byte with the committed files; CI runs it on
Debian 12 and 13.

Every tone sits exactly on an FFT bin of the full fixture length, so tests can
assert exact peak bins.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

SIGMF_VERSION = "1.2.6"
EXT = {"name": "k2websdr", "optional": False, "version": "1.0.0"}
GENERATOR = "fixtures/gen/make_synthetic.py v1"
MASK64 = (1 << 64) - 1


class XorShift64Star:
    """Integer PRNG; identical output on every platform."""

    def __init__(self, seed: int) -> None:
        if seed == 0:
            raise ValueError("seed must be nonzero")
        self.s = seed & MASK64

    def next(self) -> int:
        x = self.s
        x ^= x >> 12
        x ^= (x << 25) & MASK64
        x ^= x >> 27
        self.s = x
        return (x * 0x2545F4914F6CDD1D) & MASK64

    def small(self, amp: int) -> int:
        """Uniform integer in [-amp, amp]."""
        return (self.next() >> 32) % (2 * amp + 1) - amp


@dataclass(frozen=True)
class Tone:
    offset_hz: int     # complex: offset from center (may be negative); real: absolute in band
    amplitude: int     # peak, in int16 counts


@dataclass
class Fixture:
    name: str
    datatype: str          # ci16_le or ri16_le
    sample_rate_hz: int
    sample_count: int      # samples stored in the file
    tones: tuple[Tone, ...]
    noise_amp: int
    seed: int
    description: str
    center_hz: int | None = None          # complex only
    # (stored_index, gap_samples|None): at stored_index the source skipped gap samples
    discontinuities: tuple[tuple[int, int | None], ...] = ()
    assumed_gap_for_unknown: int = 0      # phase advance used when the gap is "unknown"
    extra_annotations: list[dict] = field(default_factory=list)


FIXTURES: tuple[Fixture, ...] = (
    Fixture(
        name="two_tone_ci16",
        datatype="ci16_le", sample_rate_hz=48_000, sample_count=12_000, center_hz=7_074_000,
        tones=(Tone(+1_500, 8_000), Tone(-3_000, 4_000)), noise_amp=64, seed=0x5EED0001,
        description="Synthetic: complex tones at +1500 Hz (8000) and -3000 Hz (4000) "
                    "around 7.074 MHz, uniform noise +/-64. Bin width 4 Hz.",
    ),
    Fixture(
        name="tone_ri16",
        datatype="ri16_le", sample_rate_hz=64_000, sample_count=16_384,
        tones=(Tone(10_000, 10_000),), noise_amp=32, seed=0x5EED0002,
        description="Synthetic: real ADC-style samples, 10 kHz tone (10000) at 64 kS/s, "
                    "frequency origin 0 Hz, uniform noise +/-32. Bin width 3.90625 Hz.",
    ),
    Fixture(
        name="gap_ci16",
        datatype="ci16_le", sample_rate_hz=48_000, sample_count=9_600, center_hz=14_074_000,
        tones=(Tone(+1_000, 8_000),), noise_amp=64, seed=0x5EED0003,
        discontinuities=((4_800, 2_412), (7_200, None)), assumed_gap_for_unknown=1_000,
        description="Synthetic: +1000 Hz tone around 14.074 MHz with two source gaps: "
                    "2412 samples (50.25 cycles) lost before sample 4800 (known), and an unknown-length "
                    "gap before sample 7200.",
    ),
)


def _source_index(fx: Fixture, stored: int) -> int:
    """Source-timeline index of a stored sample, accounting for samples lost in gaps."""
    lost = 0
    for at, gap in fx.discontinuities:
        if stored >= at:
            lost += gap if gap is not None else fx.assumed_gap_for_unknown
    return stored + lost


def _clip16(v: int) -> int:
    return max(-32768, min(32767, v))


def render_data(fx: Fixture) -> bytes:
    rng = XorShift64Star(fx.seed)
    fs = fx.sample_rate_hz
    out = bytearray()
    complex_ = fx.datatype.startswith("c")
    for k in range(fx.sample_count):
        n = _source_index(fx, k)
        i_acc = q_acc = 0.0
        for t in fx.tones:
            # Exact phase reduction: angle = 2*pi * ((f*n) mod fs) / fs
            ph = 2.0 * math.pi * ((t.offset_hz * n) % fs) / fs
            i_acc += t.amplitude * math.cos(ph)
            q_acc += t.amplitude * math.sin(ph)
        if complex_:
            out += struct.pack("<hh", _clip16(round(i_acc) + rng.small(fx.noise_amp)),
                               _clip16(round(q_acc) + rng.small(fx.noise_amp)))
        else:
            out += struct.pack("<h", _clip16(round(i_acc) + rng.small(fx.noise_amp)))
    return bytes(out)


def render_meta(fx: Fixture, data: bytes) -> bytes:
    g = {
        "core:datatype": fx.datatype,
        "core:sample_rate": fx.sample_rate_hz,
        "core:version": SIGMF_VERSION,
        "core:sha512": hashlib.sha512(data).hexdigest(),
        "core:description": fx.description,
        "core:author": "K2AAM WebSDR fixture generator",
        "core:recorder": GENERATOR,
    }
    capture: dict = {"core:sample_start": 0}
    if fx.center_hz is not None:
        capture["core:frequency"] = fx.center_hz
    annotations = []
    if fx.discontinuities:
        g["core:extensions"] = [EXT]
        for at, gap in fx.discontinuities:
            a = {"core:sample_start": at, "core:sample_count": 0, "core:label": "discontinuity"}
            if gap is not None:
                a["k2websdr:gap_samples"] = gap
            annotations.append(a)
    annotations.extend(fx.extra_annotations)
    meta = {"global": g, "captures": [capture], "annotations": annotations}
    return (json.dumps(meta, indent=2, sort_keys=True) + "\n").encode("utf-8")


def render(fx: Fixture) -> tuple[bytes, bytes]:
    data = render_data(fx)
    return render_meta(fx, data), data


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", type=Path, metavar="DIR")
    g.add_argument("--check", type=Path, metavar="DIR")
    a = p.parse_args(argv)
    bad = 0
    for fx in FIXTURES:
        meta, data = render(fx)
        if a.write:
            a.write.mkdir(parents=True, exist_ok=True)
            (a.write / f"{fx.name}.sigmf-meta").write_bytes(meta)
            (a.write / f"{fx.name}.sigmf-data").write_bytes(data)
            print(f"wrote {fx.name}: {len(data)} bytes, sha512 {hashlib.sha512(data).hexdigest()[:16]}…")
        else:
            for suffix, want in ((".sigmf-meta", meta), (".sigmf-data", data)):
                path = a.check / f"{fx.name}{suffix}"
                if not path.is_file() or path.read_bytes() != want:
                    print(f"STALE {path}", file=sys.stderr)
                    bad += 1
    if a.check:
        print("fixtures current" if not bad else f"{bad} fixture file(s) differ; run: make fixtures",
              file=sys.stderr if bad else sys.stdout)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
