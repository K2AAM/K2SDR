#!/usr/bin/env python3
"""Samples lost around each retune, from a probe --timing-log.

    timing.py LOG.csv --rate HZ [--before-s 1.0] [--after-s 1.0] [--guard-s 0.1]

For each retune, fits cumulative-samples against host time on the callbacks before the
call and, separately, after it (skipping --guard-s either side). If the device delivers
every sample, both lines describe the same clock and meet at the call time; a gap of
L samples shows up as the after-line sitting L samples below the before-line.

USB buffering jitter (a few ms per callback) averages out over hundreds of callbacks, so
the offset is good to a small fraction of a block. Also reports the library's own drop
count over the same span, the fitted rates (sanity check against nominal), and the
largest callback gap across the retune.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def load(path: Path) -> tuple[np.ndarray, list[dict]]:
    cbs, retunes = [], []
    for line in path.read_text().splitlines():
        parts = line.split(",")
        if parts[0] == "cb":
            cbs.append([int(p) for p in parts[1:5]])
        elif parts[0] == "retune":
            retunes.append({"call_ns": int(parts[1]), "return_ns": int(parts[2]), "freq_hz": int(parts[3]),
                            "result": int(parts[4])})
    return np.array(cbs, dtype=np.int64).reshape(-1, 4), retunes


def fit(t_s: np.ndarray, n: np.ndarray) -> tuple[float, float, float]:
    """Least-squares n = a + b t. Returns (a, b, residual std in samples)."""
    A = np.vstack([np.ones_like(t_s), t_s]).T
    (a, b), *_ = np.linalg.lstsq(A, n, rcond=None)
    return float(a), float(b), float(np.std(n - (a + b * t_s)))


def analyse(cbs: np.ndarray, retunes: list[dict], rate: int, before_s: float, after_s: float,
            guard_s: float) -> list[dict]:
    t = cbs[:, 0].astype(np.float64) / 1e9
    total = cbs[:, 1].astype(np.float64)
    dropped = cbs[:, 3]
    calls = [r["call_ns"] / 1e9 for r in retunes]
    out = []
    for i, r in enumerate(retunes):
        tc = calls[i]
        lo = max(tc - before_s, calls[i - 1] + guard_s if i else -np.inf)
        hi = min(tc + after_s, calls[i + 1] - guard_s if i + 1 < len(calls) else np.inf)
        b = (t >= lo) & (t <= tc - guard_s)
        a = (t >= tc + guard_s) & (t <= hi)
        rec: dict = {"retune_index": i + 1, "freq_hz": r["freq_hz"], "result": r["result"],
                     "call_ms": round((r["return_ns"] - r["call_ns"]) / 1e6, 3)}
        if b.sum() < 20 or a.sum() < 20:
            out.append({**rec, "error": "not enough callbacks either side"})
            continue
        ab, bb, sb = fit(t[b] - tc, total[b])
        aa, ba, sa = fit(t[a] - tc, total[a])
        # Both intercepts are "samples delivered by time tc"; the slopes must agree with each other.
        lost = ab - aa
        se = (sb / np.sqrt(b.sum()) + sa / np.sqrt(a.sum()))
        span = (t >= tc - guard_s) & (t <= tc + guard_s)
        idx = np.flatnonzero(span)
        gaps = np.diff(t[max(idx[0] - 1, 0):idx[-1] + 2]) if len(idx) else np.array([0.0])
        rec.update({
            "lost_samples": round(lost, 1),
            "lost_ms": round(lost / rate * 1000, 3),
            "stderr_samples": round(se, 1),
            "rate_before_hz": round(bb, 1), "rate_after_hz": round(ba, 1),
            "library_dropped_in_span": int(dropped[(t >= lo) & (t <= hi)].sum()),
            "max_callback_gap_ms": round(float(gaps.max()) * 1000, 2),
            "callbacks_fitted": [int(b.sum()), int(a.sum())],
        })
        out.append(rec)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log", type=Path)
    ap.add_argument("--rate", type=int, required=True)
    ap.add_argument("--before-s", type=float, default=1.0)
    ap.add_argument("--after-s", type=float, default=1.0)
    ap.add_argument("--guard-s", type=float, default=0.1)
    a = ap.parse_args()
    cbs, retunes = load(a.log)
    if not retunes:
        raise SystemExit("no retune rows in the timing log")
    for r in analyse(cbs, retunes, a.rate, a.before_s, a.after_s, a.guard_s):
        print(json.dumps(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
