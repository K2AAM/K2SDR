#!/usr/bin/env python3
"""Analyze Window 1 recordings.

    analyze.py carrier BASE --expect HZ [--expect HZ ...]
        For each capture segment, find the strongest tone near each expected
        absolute frequency and report its measured frequency and error in ppm
        (H6). Uses the segment's stable second half.

    analyze.py settle BASE --expect HZ [--expect HZ ...]
        For each retune (capture boundary), report how long after the recorded
        retune sample the expected carrier for the new segment becomes and stays
        dominant (H5). The recorded retune sample is approximate (counted when
        set_freq was called), so results are reported with that caveat.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from sigmf_util import load_meta, load_samples, segments  # noqa: E402


MIN_SNR_DB = 20.0   # below this a "carrier" may be a noise peak: reported, never used as a calibration


def tone_frequency(x: np.ndarray, rate: int, approx_offset: float, search_hz: float = 2000.0
                   ) -> tuple[float, float] | None:
    """(offset Hz, peak-to-median dB) of the strongest tone within ±search_hz of approx_offset."""
    n = len(x)
    if n < 1024:
        return None
    w = np.hanning(n).astype(np.float32)
    spec = np.fft.fftshift(np.fft.fft(x * w, n=4 * n))
    freqs = np.fft.fftshift(np.fft.fftfreq(4 * n, d=1.0 / rate))
    mask = np.abs(freqs - approx_offset) <= search_hz
    if not mask.any():
        return None
    mag = np.abs(spec)
    idx = np.flatnonzero(mask)
    k = idx[np.argmax(mag[idx])]
    if 0 < k < len(mag) - 1:                                  # parabolic interpolation on log magnitude
        a, b, c = np.log(mag[k - 1] + 1e-20), np.log(mag[k] + 1e-20), np.log(mag[k + 1] + 1e-20)
        p = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else 0.0
    else:
        p = 0.0
    snr_db = 20 * np.log10(mag[k] / max(np.median(mag[idx]), 1e-20))
    return float(freqs[k] + p * (freqs[1] - freqs[0])), float(snr_db)


def carrier(base: Path, expects: list[int]) -> list[dict]:
    meta, x = load_meta(base), load_samples(base)
    rate = int(meta["global"]["core:sample_rate"])
    out = []
    for start, end, f0 in segments(meta, len(x)):
        seg = x[start + (end - start) // 2:end]
        seg = seg[: min(len(seg), rate * 4)]                  # up to 4 s of the stable part
        for e in expects:
            off = e - f0
            if abs(off) >= rate / 2:
                continue
            got = tone_frequency(seg, rate, off)
            if got is None:
                continue
            offset, snr = got
            measured = f0 + offset
            out.append({"segment_freq_hz": f0, "expected_hz": e, "measured_hz": round(measured, 2),
                        "error_hz": round(measured - e, 2), "error_ppm": round((measured - e) / e * 1e6, 3),
                        "peak_snr_db": round(snr, 1),
                        "usable": snr >= MIN_SNR_DB,
                        **({} if snr >= MIN_SNR_DB else
                           {"warning": f"peak only {snr:.1f} dB above the noise: not a reliable carrier"})})
    return out


def settle(base: Path, expects: list[int], win: int = 256, hold: int = 20, thresh: float = 0.5) -> list[dict]:
    meta, x = load_meta(base), load_samples(base)
    rate = int(meta["global"]["core:sample_rate"])
    segs = segments(meta, len(x))
    out = []
    for i, (start, end, f0) in enumerate(segs[1:], 1):
        target = next((e - f0 for e in expects if abs(e - f0) < rate / 2), None)
        if target is None:
            out.append({"retune_index": i, "freq_hz": f0, "result": "no expected carrier in band"})
            continue
        k = int(round(target / rate * win)) % win
        nwin = (end - start) // win
        ok_run, settled_at = 0, None
        for j in range(nwin):
            blk = x[start + j * win:start + (j + 1) * win] * np.hanning(win)
            p = np.abs(np.fft.fft(blk)) ** 2
            frac = (p[(k - 1) % win] + p[k] + p[(k + 1) % win]) / max(p.sum(), 1e-30)
            if frac >= thresh:
                ok_run += 1
                if ok_run == hold:
                    settled_at = (j - hold + 1) * win
                    break
            else:
                ok_run = 0
        out.append({"retune_index": i, "freq_hz": f0,
                     "settle_ms": None if settled_at is None else round(settled_at / rate * 1000, 2),
                     "note": "relative to the sample counted when set_freq was called"})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["carrier", "settle"])
    ap.add_argument("base", type=Path)
    ap.add_argument("--expect", type=int, action="append", required=True)
    a = ap.parse_args()
    res = carrier(a.base, a.expect) if a.what == "carrier" else settle(a.base, a.expect)
    for r in res:
        print(json.dumps(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
