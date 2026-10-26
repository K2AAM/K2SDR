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
from sigmf_util import gap_free_runs, load_meta, load_samples, segments  # noqa: E402


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
        # Up to 4 s of the stable second half, from its longest gap-free run: a gap is
        # a phase jump that would smear the carrier peak.
        run = max(gap_free_runs(meta, start + (end - start) // 2, end), key=lambda r: r[1] - r[0])
        seg = x[run[0]:run[1]]
        seg = seg[: min(len(seg), rate * 4)]
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


def _step_index(x: np.ndarray, a: int, b: int, center: int, win: int) -> int | None:
    """Sample where total power steps from its level before `center` to its level after (None: no step)."""
    p = np.abs(x[a:b]) ** 2
    k = len(p) // win
    if k < 8:
        return None
    pw = 10 * np.log10(p[: k * win].reshape(k, win).mean(1) + 1e-30)
    c = (center - a) // win
    pre, post = np.median(pw[: max(c - 2, 1)]), np.median(pw[c + max(k // 4, 4):])
    if abs(post - pre) < 3.0:                     # levels too close to locate a step reliably
        return None
    side = (pw - (pre + post) / 2) * np.sign(post - pre) > 0
    hold = max(4, k // 20)
    j = next((j for j in range(k - hold) if side[j:j + hold].all()), None)
    return None if j is None else a + j * win


def retune(base: Path, expects: list[int], window_ms: int = 200, tol_db: float = 1.0) -> list[dict]:
    """For each retune: where the switch appears in the data and how long until the band is steady.

    step_ms: total-power step relative to the recorded capture boundary (the sample count when
      set_freq was called); positive by however many samples were already in flight.
    settle_ms: end of the last 1 ms window, within window_ms of the step, whose total power lies
      outside the range the band itself shows in steady state (from window_ms after the step to the
      end of the capture), widened by tol_db, counting only runs of >= 2 windows (a lone 1 ms
      window is a fade). 0 = nothing distinguishable from the steady band.
      A tuning transient (PLL relock junk, muting, AGC recovery) shows up here. HF signals swing
      several dB per millisecond with their own modulation, so "within N dB of the mean" tests are
      not usable; a single carrier fades and lacks SNR in 1 ms, so it is not used as the clock.
    carrier_*: whether the expected carrier is present after the switch (long-window FFT).
    """
    meta, x = load_meta(base), load_samples(base)
    rate = int(meta["global"]["core:sample_rate"])
    segs = segments(meta, len(x))
    w_step, w_ms = max(rate // 4000, 1), max(rate // 1000, 1)          # 0.25 ms and 1 ms windows
    out: list[dict] = []
    for i, (start, end, f0) in enumerate(segs[1:], 1):
        rec: dict = {"retune_index": i, "freq_hz": f0}
        a = max(segs[i - 1][0], start - rate // 50)
        b = min(end, start + rate // 20)
        step = _step_index(x, a, b, start, w_step)
        rec["step_ms"] = None if step is None else round((step - start) / rate * 1000, 2)
        s0 = step if step is not None else start
        p = np.abs(x[s0:end]) ** 2
        k = len(p) // w_ms
        if k < 2 * window_ms:
            out.append({**rec, "result": "segment too short"})
            continue
        pw = 10 * np.log10(p[: k * w_ms].reshape(k, w_ms).mean(1) + 1e-30)
        steady = pw[window_ms:]
        lo, hi = steady.min() - tol_db, steady.max() + tol_db
        out_w = (pw[:window_ms] < lo) | (pw[:window_ms] > hi)
        run = out_w[:-1] & out_w[1:]                  # >= 2 consecutive windows: a lone 1 ms window is a fade
        idx = np.flatnonzero(run)
        rec["settle_ms"] = float(idx[-1] + 2) if len(idx) else 0.0
        target = next((e - f0 for e in expects if abs(e - f0) < rate / 2), None)
        if target is not None:
            got = tone_frequency(x[s0 + rate // 10:end][: rate * 2], rate, target)
            if got:
                rec["carrier_offset_hz"], rec["carrier_snr_db"] = round(got[0], 2), round(got[1], 1)
                rec["carrier_present"] = got[1] >= MIN_SNR_DB
        out.append(rec)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["carrier", "retune", "settle"])
    ap.add_argument("base", type=Path)
    ap.add_argument("--expect", type=int, action="append", required=True)
    a = ap.parse_args()
    res = carrier(a.base, a.expect) if a.what == "carrier" else retune(a.base, a.expect)   # settle: old name
    for r in res:
        print(json.dumps(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
