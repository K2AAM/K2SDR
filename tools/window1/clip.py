#!/usr/bin/env python3
"""Cut a small committed fixture from a full recording.

    clip.py SRC_BASE OUT_BASE --center HZ --rate HZ --start-s S --seconds N

Shifts the chosen absolute center to 0 Hz, low-pass filters, decimates by an
integer factor and writes ci16_le SigMF with core:sha512. The output's
core:frequency is the chosen center (exact: integer-Hz shift of integer-rate
data). Provenance (source sha512, offsets) goes into core:description.

A clip must be one contiguous stretch of signal: it is refused if it would cross
an annotated discontinuity (lost samples), a capture boundary (retune), or the
end of the recording. Filtering across a gap would smear the jump into the
fixture, and an unannotated gap would make it lie about continuity.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from sigmf_util import discontinuities, load_meta, load_samples, segments  # noqa: E402


def lowpass(num_taps: int, cutoff: float) -> np.ndarray:
    n = np.arange(num_taps) - (num_taps - 1) / 2
    h = np.sinc(2 * cutoff * n) * np.blackman(num_taps)
    return (h / h.sum()).astype(np.float64)


class UnknownTime(Exception):
    """The clip's start time cannot be stated: an unknown-length gap lies between anchor and clip."""


def clip_datetime(meta: dict, seg: tuple[int, int, int], start: int, rate: int) -> str:
    """UTC time of the clip's first sample.

    Anchor: the datetime of the capture that contains the clip (each capture carries its own
    wall-clock stamp, so gaps before that capture are already in it and are not counted again).
    Elapsed time = stored samples from the capture start to the clip start, plus every known
    lost-sample count annotated after the capture start and at or before the clip start (a gap
    at exactly the clip start lies before its first sample). An annotated gap of unknown length
    in that span raises UnknownTime: the time is not guessed.

    The probe stamps a capture when it calls airspyhf_start/set_freq; Window 1 measured the first
    sample arriving ~85 ms after the start call and ~1490 samples silently lost per retune
    (not annotated), so this is good to about 0.1 s. Returns "" if the capture has no datetime.
    """
    cap = next((c for c in meta["captures"] if int(c["core:sample_start"]) == seg[0]), meta["captures"][0])
    stamp = cap.get("core:datetime", "")
    if not stamp:
        return ""
    lost = 0
    for at, n in discontinuities(meta):
        if seg[0] < at <= start:
            if n is None:
                raise UnknownTime(f"a discontinuity of unknown length at sample {at} precedes the clip "
                                  f"in its capture (which starts at sample {seg[0]})")
            lost += n
    t0 = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    us = round((start - seg[0] + lost) * 1_000_000 / rate)          # integer microseconds, rounded
    t = (t0 + dt.timedelta(microseconds=us)).astimezone(dt.timezone.utc)
    t = t.replace(microsecond=0) + dt.timedelta(milliseconds=round(t.microsecond / 1000))
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--center", type=int, required=True)
    ap.add_argument("--rate", type=int, required=True)
    ap.add_argument("--start-s", type=float, default=1.0)
    ap.add_argument("--seconds", type=float, default=5.0)
    a = ap.parse_args()
    meta, x = load_meta(a.src), load_samples(a.src)
    src_rate = int(meta["global"]["core:sample_rate"])
    if src_rate % a.rate:
        raise SystemExit(f"--rate must divide the source rate {src_rate}")
    segs = segments(meta, len(x))
    start = int(a.start_s * src_rate)
    seg = next((s for s in segs if s[0] <= start < s[1]), None)
    if seg is None:
        raise SystemExit("start outside recording")
    stop = start + int(a.seconds * src_rate)
    if stop > seg[1]:
        where = "the end of the recording" if seg is segs[-1] else f"a retune at sample {seg[1]}"
        raise SystemExit(f"clip {a.start_s}s+{a.seconds}s runs past {where}; choose a shorter clip")
    gaps = [(s, n) for s, n in discontinuities(meta) if start < s < stop]
    if gaps:
        s0, n0 = gaps[0]
        raise SystemExit(f"clip {a.start_s}s+{a.seconds}s crosses {len(gaps)} discontinuit"
                         f"{'y' if len(gaps) == 1 else 'ies'} (first at sample {s0}, "
                         f"{'an unknown number of' if n0 is None else n0} samples lost); "
                         "choose a clip inside a gap-free stretch")
    shift = a.center - seg[2]
    if abs(shift) + a.rate / 2 > src_rate / 2:
        raise SystemExit("requested band is outside the recording")
    y = x[start:stop].astype(np.complex128)
    n = np.arange(len(y)) + start
    y *= np.exp(-2j * np.pi * shift * n / src_rate)
    dec = src_rate // a.rate
    y = np.convolve(y, lowpass(16 * dec + 1, 0.45 / dec), mode="same")[::dec]
    try:
        when, when_note = clip_datetime(meta, seg, start, src_rate), ""
    except UnknownTime as e:
        when, when_note = "", f"; core:datetime omitted: {e}"
    peak = np.max(np.abs(np.concatenate([y.real, y.imag]))) or 1.0
    scale = 0.9 * 32767 / peak
    iq = np.empty(2 * len(y), dtype="<i2")
    iq[0::2] = np.round(y.real * scale)
    iq[1::2] = np.round(y.imag * scale)
    data = iq.tobytes()
    Path(str(a.out) + ".sigmf-data").write_bytes(data)
    out_meta = {
        "global": {
            "core:datatype": "ci16_le", "core:sample_rate": a.rate, "core:version": "1.2.6",
            "core:sha512": hashlib.sha512(data).hexdigest(),
            "core:description": (f"Real Airspy HF+ recording (Window 1), clipped: source sha512 "
                                 f"{meta['global'].get('core:sha512', '?')[:16]}..., source center {seg[2]} Hz "
                                 f"@ {src_rate} S/s, {a.start_s}s+{a.seconds}s, shifted to {a.center} Hz, "
                                 f"decimated x{dec}, normalized to 90% full scale{when_note}"),
            "core:recorder": "tools/window1/clip.py",
            "core:hw": meta["global"].get("core:hw", ""),
        },
        "captures": [{"core:sample_start": 0, "core:frequency": a.center,
                      **({"core:datetime": when} if when else {})}],
        "annotations": [],
    }
    Path(str(a.out) + ".sigmf-meta").write_text(json.dumps(out_meta, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"out": str(a.out), "samples": len(y), "bytes": len(data), "datetime": when or None,
                      **({"datetime_note": when_note[2:]} if when_note else {})}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
