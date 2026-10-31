#!/usr/bin/env python3
"""Test double for rx888_stream (CI / development only; never talks to hardware).

Takes the options window_r1.py uses and writes real int16 samples to stdout at the requested
rate in real time (use small rates in tests). What it "receives" is scripted by environment:

  MOCK_TONES      "f_hz:dbfs,..."  HF carriers (nominal frequencies)
  MOCK_PPM        ADC clock error in ppm: a carrier at f appears at f / (1 + ppm·1e-6)
  MOCK_NOISE_DBFS noise floor (rms, before gain), default -70
  MOCK_VHF_IF     where the VHF tuner's output lands in the ADC band (Hz), default 300000
  MOCK_DIE_AT_S   stop streaming after N s and exit 1 ("unplugged"); MOCK_HANG=1: stall instead
Gain model: +0.25 dB per VGA step (high mode +10 dB), -0.5 dB per attenuator step, PGA +3 dB.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="hf")
    ap.add_argument("-f", "--firmware")
    ap.add_argument("-d", "--dither", action="store_true")
    ap.add_argument("-r", "--randomize", action="store_true")
    ap.add_argument("-s", "--sample-rate", type=int, default=50_000_000)
    ap.add_argument("-g", "--gain", type=int, default=1)
    ap.add_argument("-m", "--gain-mode", default="high")
    ap.add_argument("-a", "--attenuation", type=int, default=0)
    ap.add_argument("--pga", action="store_true")
    ap.add_argument("-o", "--output")
    ap.add_argument("--measure", action="store_true")
    ap.add_argument("--frequency", type=int, default=145_000_000)
    ap.add_argument("--vhf-lna", type=int, default=28)
    ap.add_argument("--vhf-vga", type=int, default=15)
    a = ap.parse_args()
    fs = a.sample_rate
    gain_db = 0.25 * a.gain + (10 if a.gain_mode == "high" else 0) - 0.5 * a.attenuation + (3 if a.pga else 0)
    g = 10 ** (gain_db / 20)
    ppm = float(os.environ.get("MOCK_PPM", "0"))
    tones = []
    if a.cmd == "vhf":
        tones = [(float(os.environ.get("MOCK_VHF_IF", "300000")), -30.0)]
    else:
        for item in filter(None, os.environ.get("MOCK_TONES", "").split(",")):
            f, lv = item.split(":")
            tones.append((float(f) / (1 + ppm * 1e-6), float(lv)))
    noise = 10 ** (float(os.environ.get("MOCK_NOISE_DBFS", "-70")) / 20) * 32767
    die = float(os.environ.get("MOCK_DIE_AT_S", "0"))
    print(f"Attenuation: {a.attenuation}\nGain: {a.gain}", file=sys.stderr, flush=True)
    rng = np.random.default_rng(7)
    out = sys.stdout.buffer
    chunk = max(1, fs // 20)
    n0, t0 = 0, time.monotonic()
    last_measure = t0
    try:
        while True:
            el = time.monotonic() - t0
            if die and el >= die:
                if os.environ.get("MOCK_HANG") == "1":
                    time.sleep(3600)
                print("Transfer error: device disconnected", file=sys.stderr, flush=True)
                return 1
            n = np.arange(n0, n0 + chunk, dtype=np.float64)
            x = rng.standard_normal(chunk) * noise
            for f, lv in tones:
                x += np.sqrt(2) * 10 ** (lv / 20) * 32767 * np.cos(2 * np.pi * f * n / fs)
            x = np.clip(np.round(x * g), -32768, 32767).astype("<i2")
            out.write(x.tobytes())
            n0 += chunk
            if a.measure and time.monotonic() - last_measure >= 1:
                last_measure = time.monotonic()
                print(f"Sample rate: {fs * (1 + ppm * 1e-6):.1f} Hz", file=sys.stderr, flush=True)
            ahead = n0 / fs - (time.monotonic() - t0)
            if ahead > 0:
                time.sleep(ahead)
    except (BrokenPipeError, KeyboardInterrupt):
        return 0


if __name__ == "__main__":
    sys.exit(main())
