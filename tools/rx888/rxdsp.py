"""Signal helpers for the RX888 window tools (numpy only; offline analysis, not the worker).

`extract_iq` cuts a narrow complex band out of a wide real recording by fast convolution
(overlap-save on real FFT blocks): the M2 front-end idea in its simplest form, used here to make
small CI clips and to measure carriers. Output rate = fs·M/N must be an integer, and the clip
centre is exact integer Hz (a residual mix corrects the bin grid).
"""

from __future__ import annotations

import numpy as np


def out_rate_for(fs: int, n: int, m: int) -> int:
    if (fs * m) % n:
        raise ValueError(f"fs·M/N is not an integer for fs={fs}, N={n}, M={m}")
    return fs * m // n


def extract_iq(x: np.ndarray, fs: int, center_hz: int, n: int, m: int, max_out: int | None = None) -> np.ndarray:
    """Complex baseband around `center_hz` from real samples `x` at `fs`: N-point real FFT blocks
    with 50 % overlap, M bins kept (tapered edges), M-point inverse FFT, middle half of each block
    kept. Amplitude: a real carrier A·cos(...) comes out as A·e^{j...}. Starts at input sample N/4."""
    if n % 4 or m % 4 or m > n // 2:
        raise ValueError("N and M must be multiples of 4 and M <= N/2")
    rate = out_rate_for(fs, n, m)
    kc = int(round(center_hz * n / fs))
    if kc - m // 2 < 0 or kc + m // 2 > n // 2:
        raise ValueError("band outside 0..fs/2")
    delta = center_hz - kc * fs / n
    taper = max(1, m // 16)                                  # raised-cosine edges, flat middle
    w = np.ones(m)
    ramp = 0.5 - 0.5 * np.cos(np.pi * (np.arange(taper) + 0.5) / taper)
    w[:taper], w[-taper:] = ramp, ramp[::-1]
    hop = n // 2
    blocks = (len(x) - n) // hop + 1
    if max_out is not None:
        blocks = min(blocks, -(-max_out // (m // 2)))
    out = np.empty(max(0, blocks) * (m // 2), np.complex128)
    for b in range(blocks):
        seg = np.asarray(x[b * hop: b * hop + n], np.float64)      # rectangular: overlap-save
        spec = np.fft.rfft(seg)
        y = spec[kc - m // 2: kc + m // 2] * w
        z = np.fft.ifft(np.fft.ifftshift(y)) * (2 * m / n)
        if (kc * b) % 2:                                     # block start b·N/2: phase of kc bins
            z = -z
        out[b * (m // 2):(b + 1) * (m // 2)] = z[m // 4: 3 * m // 4]
    t = (n // 4 + np.arange(len(out)) * (n / m)) / fs
    out *= np.exp(-2j * np.pi * delta * t)
    out = out[:max_out] if max_out is not None else out
    assert rate > 0
    return out.astype(np.complex64)


def psd_real(x: np.ndarray, fs: int, nfft: int, max_blocks: int = 64) -> tuple[np.ndarray, np.ndarray]:
    """(frequencies 0..fs/2, mean power per bin in dBFS) of int16 real samples, Hann-windowed."""
    w = np.hanning(nfft)
    norm = (32768.0 * w.sum() / 2) ** 2                     # full-scale sine -> 0 dBFS at its bin
    acc = np.zeros(nfft // 2 + 1)
    blocks = min(max_blocks, max(1, len(x) // nfft))
    step = max(nfft, (len(x) - nfft) // max(1, blocks - 1)) if blocks > 1 else nfft
    used = 0
    for i in range(blocks):
        seg = np.asarray(x[i * step: i * step + nfft], np.float64)
        if len(seg) < nfft:
            break
        acc += np.abs(np.fft.rfft(seg * w)) ** 2
        used += 1
    p = acc / max(1, used) / norm
    return np.arange(nfft // 2 + 1) * fs / nfft, 10 * np.log10(p + 1e-30)


def carrier_offset(iq: np.ndarray, rate: int) -> tuple[float, float]:
    """(frequency offset in Hz of the strongest line, its level over the median in dB), with
    parabolic interpolation on the log spectrum of the whole clip."""
    n = len(iq)
    spec = np.abs(np.fft.fftshift(np.fft.fft(iq * np.hanning(n)))) ** 2
    f = (np.arange(n) - n // 2) * rate / n
    k = int(np.argmax(spec))
    if 0 < k < n - 1:
        a, b, c = np.log(spec[k - 1:k + 2] + 1e-30)
        k_frac = k + 0.5 * (a - c) / (a - 2 * b + c)
    else:
        k_frac = k
    return float((k_frac - n // 2) * rate / n), float(10 * np.log10(spec[k] / np.median(spec)))


def write_sigmf(base, iq: np.ndarray, rate: int, center_hz: int, description: str, hw: str) -> dict:
    """ci16_le SigMF clip (normalized to 90 % full scale), sha512 in the meta. Returns the meta."""
    import hashlib
    import json
    from pathlib import Path
    peak = float(np.max(np.abs(np.concatenate([iq.real, iq.imag])))) or 1.0
    s = 0.9 * 32767 / peak
    raw = np.empty(2 * len(iq), "<i2")
    raw[0::2], raw[1::2] = np.round(iq.real * s), np.round(iq.imag * s)
    data = raw.tobytes()
    Path(str(base) + ".sigmf-data").write_bytes(data)
    meta = {"global": {"core:datatype": "ci16_le", "core:sample_rate": int(rate), "core:version": "1.2.6",
                       "core:sha512": hashlib.sha512(data).hexdigest(), "core:description": description,
                       "core:hw": hw, "core:recorder": "tools/rx888/window_r1.py"},
            "captures": [{"core:sample_start": 0, "core:frequency": int(center_hz)}], "annotations": []}
    Path(str(base) + ".sigmf-meta").write_text(json.dumps(meta, indent=2) + "\n")
    return meta
