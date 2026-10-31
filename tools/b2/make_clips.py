#!/usr/bin/env python3
"""B2 clips from the Window 1 raw recordings: no hardware, OpenWebRX keeps running.

    control/.venv/bin/python tools/b2/make_clips.py

M1 acceptance B2 asks that every mode demodulates real recorded signals in CI. AM (WWV) and USB
(FT8) have real clips; this cuts the others from recordings already on the Optiplex
(`window1-results/20261026-181353/rec/`, read-only):

  wfm  strongest FM station in `fm` (100.1 MHz, 912k)      -> 228 kS/s, 3 s
  am   strongest MW carrier in `mw` (1.000 MHz, 912k)       -> 48 kS/s, 5 s  (broadcast AM)
  cw   most clearly keyed narrow signal, 7.025-7.060 MHz    -> 48 kS/s, 6 s  (from `40m`)
  lsb  3 kHz band with the most speech-like activity,       -> 48 kS/s, 8 s  (from `40m`)
       7.125-7.214 MHz, dial at its upper edge

It prints what it chose and why, writes clips with tools/window1/clip.py (provenance and sha512 in
the SigMF meta), and packs them with a summary into /tmp/k2-b2-<stamp>.tar.gz (a few MB). The
picks are checked again with the real demodulators before any clip becomes a CI fixture. NFM needs
a live window (NOAA): nothing NFM is in the Window 1 recordings.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/window1"))
from sigmf_util import load_meta, load_samples  # noqa: E402


def psd_frames(x: np.ndarray, rate: int, nfft: int, frames: int) -> tuple[np.ndarray, np.ndarray]:
    """(frequency offsets, power[frames, nfft]) from `frames` evenly spaced FFTs."""
    step = max(nfft, (len(x) - nfft) // frames)
    w = np.hanning(nfft).astype(np.float32)
    rows = [np.abs(np.fft.fftshift(np.fft.fft(x[i:i + nfft] * w))) ** 2
            for i in range(0, len(x) - nfft, step)][:frames]
    return (np.arange(nfft) - nfft // 2) * rate / nfft, np.array(rows)


def band(f, lo, hi):
    return (f >= lo) & (f < hi)


def pick_wfm(x, rate, center):
    f, p = psd_frames(x, rate, 8192, 200)
    avg = p.mean(0)
    noise = np.median(avg)
    best = None
    for st in range(87_900_000, 108_000_000, 200_000):
        off = st - center
        if abs(off) + 114_000 > rate / 2:                    # the 228k clip must fit, centred on it
            continue
        snr = 10 * np.log10(avg[band(f, off - 75_000, off + 75_000)].mean() / noise)
        if best is None or snr > best[1]:
            best = (st, snr)
    return {"frequency_hz": best[0], "why": f"strongest FM channel in the span, {best[1]:.1f} dB over the median"}


def pick_am(x, rate, center):
    f, p = psd_frames(x, rate, 65536, 60)
    avg = p.mean(0)
    noise = np.median(avg)
    best = None
    for st in range(530_000, 1_710_000, 10_000):
        off = st - center
        if abs(off) + 6_000 > rate / 2:
            continue
        carrier = avg[band(f, off - 30, off + 30)].max()
        snr = 10 * np.log10(carrier / noise)
        if best is None or snr > best[1]:
            best = (st, snr)
    return {"frequency_hz": best[0], "why": f"strongest MW carrier, {best[1]:.1f} dB over the median"}


def pick_cw(x, rate, center):
    nfft = 8192                                              # 23 Hz bins at 192k
    f, p = psd_frames(x, rate, nfft, 400)
    sel = np.where(band(f, 7_025_000 - center, 7_060_000 - center))[0]
    noise = np.median(p[:, sel])
    best = None
    for i in sel:
        col = p[:, i]
        on = np.percentile(col, 90) / noise                  # keyed: loud part of the time...
        off = np.percentile(col, 20) / noise                 # ...and quiet the rest
        score = 10 * np.log10(on) - max(0.0, 10 * np.log10(off))
        if 10 * np.log10(on) > 15 and (best is None or score > best[1]):
            best = (int(round(center + f[i])), score, 10 * np.log10(on), 10 * np.log10(off))
    if best is None:
        return None
    return {"frequency_hz": best[0],
            "why": f"keyed carrier: on {best[2]:.1f} dB, off {best[3]:.1f} dB over the noise"}


def pick_lsb(x, rate, center):
    nfft = 4096                                              # 47 Hz bins
    f, p = psd_frames(x, rate, nfft, 600)
    noise = np.median(p)
    best = None
    for dial in range(7_128_000, 7_214_000, 500):
        b = band(f, dial - 2_700 - center, dial - 300 - center)
        e = p[:, b].mean(1) / noise                          # band power over time
        upper = p[:, band(f, dial + 300 - center, dial + 2_700 - center)].mean(1) / noise
        if e.mean() < 2:
            continue
        # Speech: syllabic variation (spread of the band power over time) in the lower sideband,
        # and clearly more energy below the dial than above it.
        var = np.std(10 * np.log10(e))
        side = 10 * np.log10(e.mean() / max(upper.mean(), 1e-9))
        score = var + 0.5 * side
        if side > 6 and (best is None or score > best[1]):
            frame = int(np.argmax(np.convolve(e, np.ones(80), "same")))     # most active ~8 s
            best = (dial, score, var, side, frame)
    if best is None:
        return None
    start_s = max(0.5, best[4] * (len(x) / len(p)) / rate - 4)
    return {"frequency_hz": best[0], "start_s": round(start_s, 1),
            "why": f"speech-like: {best[2]:.1f} dB syllabic spread, lower sideband {best[3]:.1f} dB over upper"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rec-dir", type=Path, default=ROOT / "window1-results/20261026-181353/rec")
    ap.add_argument("--out", type=Path, default=Path.home() / "b2-clips" / time.strftime("%Y%m%d-%H%M%S"))
    a = ap.parse_args()
    if not (a.rec_dir / "40m.sigmf-meta").is_file():
        raise SystemExit(f"Window 1 recordings not found in {a.rec_dir}")
    a.out.mkdir(parents=True, exist_ok=False)
    plan = [("wfm", "fm", pick_wfm, 228_000, 3.0), ("am_mw", "mw", pick_am, 48_000, 5.0),
            ("cw", "40m", pick_cw, 48_000, 6.0), ("lsb", "40m", pick_lsb, 48_000, 8.0)]
    summary = {}
    for name, rec, pick, out_rate, seconds in plan:
        src = a.rec_dir / rec
        meta = load_meta(src)
        rate = int(meta["global"]["core:sample_rate"])
        center = int(meta["captures"][0]["core:frequency"])
        print(f"{name}: analysing {rec} ({center} Hz, {rate} S/s) ...", flush=True)
        x = load_samples(src)
        choice = pick(x, rate, center)
        duration = len(x) / rate
        del x
        if choice is None:
            print(f"  {name}: no suitable signal found; skipped")
            summary[name] = {"skipped": "no suitable signal"}
            continue
        start = round(min(choice.get("start_s", 1.0), duration - seconds - 0.1), 1)   # inside the recording
        if start < 0:
            raise SystemExit(f"{rec} is only {duration:.1f} s long; a {seconds} s clip does not fit")
        # The clip's centre stays inside the recording's span (CW near 7.025 MHz and LSB near
        # 7.214 MHz sit close to the edges of the 40 m recording); the signal is then off-centre,
        # at frequency_hz - clip_center_hz, which the tests use as the channel offset.
        margin = out_rate // 2 + 1_000
        clip_center = int(min(max(choice["frequency_hz"], center - rate // 2 + margin), center + rate // 2 - margin))
        choice["clip_center_hz"] = clip_center
        base = a.out / f"airspyhf_{name}_{choice['frequency_hz']}"
        subprocess.run([sys.executable, str(ROOT / "tools/window1/clip.py"), str(src), str(base),
                        "--center", str(clip_center), "--rate", str(out_rate),
                        "--start-s", str(start), "--seconds", str(seconds)], check=True)
        choice.update(source=rec, rate=out_rate, start_s=start, seconds=seconds, clip=base.name)
        summary[name] = choice
        print(f"  {name}: {choice['frequency_hz']} Hz, {choice['why']}")
    (a.out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    bundle = Path("/tmp") / f"k2-b2-{a.out.name}.tar.gz"
    with tarfile.open(bundle, "w:gz") as t:
        for p in sorted(a.out.iterdir()):
            t.add(p, arcname=f"{a.out.name}/{p.name}")
    print(f"\nBundle: {bundle} ({bundle.stat().st_size / 1e6:.1f} MB) — send this to Claude")
    return 0


if __name__ == "__main__":
    sys.exit(main())
