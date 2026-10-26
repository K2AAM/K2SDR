"""Real Airspy HF+ clips from Window 1 (fixtures/real/airspyhf): integrity and the signals they contain.

These are recordings of real transmissions (2026-10-26, K2AAM, Debian 13 Optiplex), so the
assertions are about what was measured on them, with margins, not synthetic exactness.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from k2websdr.replay import sigmf as k2sigmf

REAL = Path(__file__).resolve().parents[2] / "fixtures/real/airspyhf"
NAMES = ["airspyhf_wwv10_48k", "airspyhf_ft8_40m_48k", "airspyhf_ssb_40m_48k"]


def samples(name: str) -> tuple[np.ndarray, int, int]:
    rec = k2sigmf.load(REAL / f"{name}.sigmf-meta", require_sha512=True)     # verifies core:sha512
    raw = np.fromfile(rec.data_path, dtype="<i2").astype(np.float64)
    return raw[0::2] + 1j * raw[1::2], rec.sample_rate_hz, rec.freq_origin_hz


@pytest.mark.parametrize("name", NAMES)
def test_real_fixture_loads_with_integer_hz_and_no_gaps(name):
    x, rate, center = samples(name)
    meta = json.loads((REAL / f"{name}.sigmf-meta").read_text())
    assert rate == 48000 and isinstance(center, int)
    assert meta["annotations"] == []                        # clips are contiguous (clip.py refuses gaps)
    assert "Airspy HF+" in meta["global"]["core:hw"]
    assert np.max(np.abs(np.concatenate([x.real, x.imag]))) < 32767   # not clipped


def test_wwv_carrier_is_where_the_receiver_says_within_a_few_hz():
    # WWV 10 MHz carrier sits at 10_000_000 Hz; the clip is centred at 10_001_000 Hz -> -1000 Hz.
    # Window 1 measured the receiver ~0.9 Hz low at 10 MHz; allow 3 Hz.
    x, rate, center = samples("airspyhf_wwv10_48k")
    n = 1 << 20
    spec = np.abs(np.fft.fftshift(np.fft.fft(x * np.hanning(len(x)), n)))
    f = np.fft.fftshift(np.fft.fftfreq(n, 1 / rate))
    band = np.abs(f + 1000) < 50
    peak = f[band][np.argmax(spec[band])]
    assert abs(center + peak - 10_000_000) < 3.0, center + peak
    assert 20 * np.log10(spec[band].max() / np.median(spec)) > 30


def test_ft8_clip_has_its_energy_in_the_ft8_subband():
    # Dial 7.074 MHz USB: FT8 audio 200-3000 Hz -> 7_074_200..7_077_000 Hz -> -1300..+1500 Hz from centre.
    x, rate, center = samples("airspyhf_ft8_40m_48k")
    assert center == 7_075_500 and len(x) == 15 * rate              # one full 15 s FT8 slot
    p = np.abs(np.fft.fftshift(np.fft.fft(x))) ** 2
    f = np.fft.fftshift(np.fft.fftfreq(len(x), 1 / rate))
    inside = p[(f > -1300) & (f < 1500)].mean()
    outside = p[(np.abs(f) > 3000) & (np.abs(f) < 20000)].mean()
    assert 10 * np.log10(inside / outside) > 10
    decodes = [ln for ln in (REAL / "airspyhf_ft8_40m_48k.decodes.txt").read_text().splitlines()
               if ln and not ln.startswith("#")]
    assert len(decodes) == 22
