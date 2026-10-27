"""The pinned liquid-dsp build: provenance, no FFTW, and the M1 DSP blocks it provides.

Uses build/worker/ksdr-dsp-check (worker/dsp/dsp_check.cpp), which runs liquid's FFT, NCO,
multi-stage resampler, FIR/IIR filters, frequency demodulator and AGC on samples piped through
it. Reference results come from numpy and from real Airspy HF+ recordings (fixtures/real).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build/worker"
PREFIX = ROOT / "build/third_party/liquid-dsp"
CHECK = BUILD / "ksdr-dsp-check"
REAL = ROOT / "fixtures/real/airspyhf"
AUDIO = 12000

sys.path.insert(0, str(ROOT / "tools"))
import fetch_liquid as pin  # noqa: E402


def dsp(*args, data: bytes = b"", timeout=120) -> bytes:
    r = subprocess.run([str(CHECK), *map(str, args)], input=data, capture_output=True, timeout=timeout)
    assert r.returncode == 0, r.stderr.decode()
    return r.stdout


def chain(x: np.ndarray, rate: int, offset: float, mode: str, *extra) -> np.ndarray:
    out = dsp("chain", "--in-rate", rate, "--offset", offset, "--mode", mode, *extra,
              data=x.astype(np.complex64).tobytes())
    return np.frombuffer(out, np.float32)


def tone(rate: int, seconds: float, f: float, amp: float = 0.3) -> np.ndarray:
    n = np.arange(int(rate * seconds))
    return amp * np.exp(2j * np.pi * f * n / rate)


def psd(a: np.ndarray, fs: int = AUDIO) -> tuple[np.ndarray, np.ndarray]:
    p = np.abs(np.fft.rfft(a * np.hanning(len(a)))) ** 2
    return np.fft.rfftfreq(len(a), 1 / fs), p


def peak_hz(a: np.ndarray, fs: int = AUDIO) -> float:
    f, p = psd(a, fs)
    p[f < 50] = 0
    return float(f[np.argmax(p)])


def rms_db(a: np.ndarray) -> float:
    return 10 * np.log10(np.mean(a.astype(np.float64) ** 2) + 1e-30)


def settled(a: np.ndarray, skip_s: float = 0.25) -> np.ndarray:
    return a[int(skip_s * AUDIO):]


# ----------------------------------------------------------------- provenance / dependencies

def test_both_binaries_run_the_pinned_liquid_dsp():
    v = json.loads(dsp("version"))
    assert v == {"liquid_header": pin.PIN_VERSION, "liquid_library": pin.PIN_VERSION, "pinned": pin.PIN_VERSION,
                 "fft": "internal"}
    out = subprocess.run([str(BUILD / "ksdr-worker"), "--version"], capture_output=True, text=True, check=True)
    assert f"liquid-dsp {pin.PIN_VERSION}" in out.stdout


def test_build_info_records_source_options_and_license():
    info = json.loads((PREFIX / "k2-build-info.json").read_text())
    assert (info["version"], info["commit"], info["source_manifest_sha256"]) == \
        (pin.PIN_VERSION, pin.PIN_COMMIT, pin.PIN_MANIFEST_SHA256)
    assert "-DFIND_FFTW=OFF" in info["cmake_options"] and info["license"] == "MIT"
    lic = (PREFIX / "share/doc/liquid-dsp/LICENSE").read_text()
    assert "Permission is hereby granted, free of charge" in lic and "Joseph Gaeddert" in lic
    assert (ROOT / "third_party/liquid-dsp/LICENSE").read_text() == lic          # notice kept in the repo


def test_dependency_closure_has_no_fftw_and_only_the_pinned_liquid():
    flags = []
    if os.environ.get("K2_EXPECT_FFTW_INSTALLED") == "1":                       # CI installs both on purpose
        flags += ["--expect-fftw-installed", "--expect-debian-liquid"]
    r = subprocess.run([sys.executable, str(ROOT / "tools/check_dsp_deps.py"), str(BUILD), str(PREFIX), *flags],
                       capture_output=True, text=True)
    rep = json.loads(r.stdout)
    assert r.returncode == 0 and rep["ok"], rep["failures"]
    for b in rep["binaries"].values():
        assert b["liquid_inputs"] == [str((PREFIX / "lib/libliquid.a").resolve())]


def test_checker_catches_statically_linked_fftw_that_ldd_cannot_see(tmp_path):
    """A binary with FFTW linked in statically has clean NEEDED entries; the checker must still fail."""
    fake = tmp_path / "fftw_fake.c"
    fake.write_text("int fftwf_execute(void *p) { return p != 0; }\nint fftwf_cleanup(void) { return 0; }\n")
    subprocess.run(["cc", "-c", "-fPIC", str(fake), "-o", str(tmp_path / "fftw_fake.o")], check=True)
    subprocess.run(["ar", "rcs", str(tmp_path / "libfftw3f.a"), str(tmp_path / "fftw_fake.o")], check=True)
    prog = tmp_path / "prog.c"
    prog.write_text('#include <complex.h>\n#include <stdio.h>\n#include <liquid/liquid.h>\n'
                    'int fftwf_execute(void *);\n'
                    'int main(int c, char **v) { (void)v; if (c > 9) fftwf_execute(0);\n'
                    '  printf("liquid-dsp %s\\n", liquid_libversion()); return 0; }\n')
    build = tmp_path / "build/worker"
    build.mkdir(parents=True)
    (tmp_path / "build/third_party").mkdir()
    (tmp_path / "build/third_party/liquid-dsp-build").symlink_to(ROOT / "build/third_party/liquid-dsp-build")
    for name in ("ksdr-worker", "ksdr-dsp-check"):
        subprocess.run(["cc", str(prog), "-I", str(PREFIX / "include"), str(PREFIX / "lib/libliquid.a"),
                        str(tmp_path / "libfftw3f.a"), "-lm", f"-Wl,-Map={build / name}.map", "-o", str(build / name)],
                       check=True)
    r = subprocess.run([sys.executable, str(ROOT / "tools/check_dsp_deps.py"), str(build), str(PREFIX)],
                       capture_output=True, text=True)
    rep = json.loads(r.stdout)
    assert r.returncode == 1 and not rep["ok"]
    assert all("fftw" not in " ".join(b["needed"]) for b in rep["binaries"].values())   # ldd view: clean
    text = "\n".join(rep["failures"])
    assert "FFTW symbols linked in" in text and "linker inputs include FFTW" in text


# --------------------------------------------------------------------------------- FFT

@pytest.mark.parametrize("n", [1024, 2048, 3000, 4096, 8192])
def test_fft_matches_numpy(n):
    rng = np.random.default_rng(n)
    frames = 3
    x = (rng.standard_normal((frames, n)) + 1j * rng.standard_normal((frames, n))).astype(np.complex64)
    y = np.frombuffer(dsp("fft", n, data=x.tobytes()), np.complex64).reshape(frames, n)
    ref = np.fft.fft(x.astype(np.complex128), axis=1)
    err = np.max(np.abs(y - ref)) / np.max(np.abs(ref))
    assert err < 2e-5, err


def test_fft_of_a_windowed_tone_lands_in_the_right_bin():
    n, rate = 4096, 384000
    f = 12_345.0
    x = (np.hanning(n) * np.exp(2j * np.pi * f * np.arange(n) / rate)).astype(np.complex64)
    y = np.frombuffer(dsp("fft", n, data=x.tobytes()), np.complex64)
    k = int(np.argmax(np.abs(y)))
    assert k == round(f / rate * n)


# --------------------------------------------------------------- channel filter / resampler

def test_channel_resampler_rate_and_out_of_channel_rejection():
    rate, off = 384000, 50_000
    inband = chain(tone(rate, 2.0, off + 1000), rate, off, "usb")
    outband = chain(tone(rate, 2.0, off + 20_000), rate, off, "usb")
    assert abs(len(inband) - 2 * AUDIO) <= 64                          # 384 kS/s -> 12 kS/s
    assert abs(peak_hz(settled(inband)) - 1000) < 5
    assert rms_db(settled(inband)) - rms_db(settled(outband)) > 60      # 20 kHz away: gone


@pytest.mark.parametrize("rate", [192000, 650000, 912000])
def test_channel_works_at_integer_and_fractional_decimation(rate):
    a = chain(tone(rate, 1.0, -30_000 + 800), rate, -30_000, "usb")
    assert abs(len(a) - AUDIO) <= 64 and abs(peak_hz(settled(a)) - 800) < 5


# ------------------------------------------------------------------------- demodulators

def test_usb_and_lsb_select_their_sideband():
    rate, off = 384000, 0
    upper, lower = tone(rate, 1.0, off + 1000), tone(rate, 1.0, off - 1000)
    usb_u, usb_l = settled(chain(upper, rate, off, "usb")), settled(chain(lower, rate, off, "usb"))
    lsb_u, lsb_l = settled(chain(upper, rate, off, "lsb")), settled(chain(lower, rate, off, "lsb"))
    assert abs(peak_hz(usb_u) - 1000) < 5 and abs(peak_hz(lsb_l) - 1000) < 5
    assert rms_db(usb_u) - rms_db(usb_l) > 50
    assert rms_db(lsb_l) - rms_db(lsb_u) > 50


@pytest.mark.parametrize("bfo", [700, 600])
def test_cw_carrier_becomes_the_bfo_pitch(bfo):
    rate, off = 192000, 7_000
    a = settled(chain(tone(rate, 1.0, off), rate, off, "cw", "--bfo", bfo))
    assert abs(peak_hz(a) - bfo) < 5


def test_am_envelope_recovers_the_tone_without_dc():
    rate, off, n = 384000, 25_000, np.arange(384000)
    x = 0.2 * (1 + 0.5 * np.cos(2 * np.pi * 1000 * n / rate)) * np.exp(2j * np.pi * off * n / rate)
    a = settled(chain(x, rate, off, "am"), 0.5)
    f, p = psd(a)
    k1 = np.argmin(np.abs(f - 1000))
    assert abs(peak_hz(a) - 1000) < 5
    assert abs(np.mean(a)) < 0.02 * np.std(a)                          # DC blocked
    k2 = np.argmin(np.abs(f - 2000))
    assert 10 * np.log10(p[k1] / p[k2]) > 30                            # clean envelope, low 2nd harmonic


def fm(rate: int, seconds: float, carrier: float, fmod: float, dev: float) -> np.ndarray:
    n = np.arange(int(rate * seconds))
    phase = 2 * np.pi * carrier * n / rate + (dev / fmod) * np.sin(2 * np.pi * fmod * n / rate)
    return 0.3 * np.exp(1j * phase)


def test_nfm_output_follows_deviation_linearly():
    rate, off = 384000, -40_000
    a3 = settled(chain(fm(rate, 1.0, off, 1000, 3000), rate, off, "nfm"))
    a15 = settled(chain(fm(rate, 1.0, off, 1000, 1500), rate, off, "nfm"))
    assert abs(peak_hz(a3) - 1000) < 5
    ratio = np.std(a15) / np.std(a3)
    assert abs(ratio - 0.5) < 0.05, ratio


def deemph(f):  # one-pole 75 us de-emphasis
    return 1 / np.sqrt(1 + (2 * np.pi * f * 75e-6) ** 2)


def wfm_level_db(fmod: float) -> float:
    rate, off = 912000, 100_000
    a = settled(chain(fm(rate, 1.0, off, fmod, 75_000), rate, off, "wfm"))
    assert abs(peak_hz(a) - fmod) < 5
    return 20 * np.log10(np.std(a))


def test_wfm_demodulates_with_75us_deemphasis():
    got = wfm_level_db(3000) - wfm_level_db(1000)
    expect = 20 * np.log10(deemph(3000) / deemph(1000))                # about -3.9 dB
    assert abs(got - expect) < 0.3, (got, expect)


def test_audio_band_at_12ks_is_flat_to_4khz():
    # De-emphasis removed: what is left is the 12 kS/s audio resampler (measured: -0.5 dB at 4 kHz,
    # -2 dB at 5 kHz, -3.3 dB at 5.5 kHz). M1 audio is good to about 4.5 kHz.
    base = wfm_level_db(1000) - 20 * np.log10(deemph(1000))
    rel = {f: wfm_level_db(f) - 20 * np.log10(deemph(f)) - base for f in (2000, 4000, 5500)}
    assert abs(rel[2000]) < 0.2 and -1.0 < rel[4000] < 0.2 and -5.0 < rel[5500] < -2.0, rel


# --------------------------------------------------------------------------------- AGC

def agc_step(mode: str) -> np.ndarray:
    n = np.arange(4 * AUDIO)
    x = np.sin(2 * np.pi * 500 * n / AUDIO)
    x[: 3 * AUDIO // 2] *= 0.01                                        # 1.5 s quiet, then +40 dB
    return np.frombuffer(dsp("agc", "--mode", mode, data=x.astype(np.float32).tobytes()), np.float32)


def settle_time_s(y: np.ndarray, step: int) -> float:
    """Time after the step until 10 ms levels stay within 3 dB of the final level."""
    win = AUDIO // 100
    final = rms_db(y[-win:])
    last_out = step
    for i in range(step, len(y) - win, win):
        if abs(rms_db(y[i:i + win]) - final) >= 3:
            last_out = i + win
    return (last_out - step) / AUDIO


def test_agc_levels_quiet_and_loud_input_and_speeds_are_ordered():
    step = 3 * AUDIO // 2
    times = {}
    for mode in ("fast", "medium", "slow"):
        y = agc_step(mode)
        quiet, loud = rms_db(y[step - AUDIO // 4:step]), rms_db(y[-AUDIO // 4:])
        assert abs(quiet - loud) < 1, (mode, quiet, loud)              # 40 dB input step -> same level
        assert abs(loud - 20 * np.log10(0.25)) < 1                     # scale 0.25 = output RMS
        times[mode] = settle_time_s(y, step)
    # measured: fast 0.04 s, medium 0.23 s, slow 1.16 s after a +40 dB step
    assert times["fast"] < 0.1 < times["medium"] < 0.5 < times["slow"] < 1.5, times


# --------------------------------------------------------------------- real recordings

def real_cf32(name: str) -> np.ndarray:
    raw = np.fromfile(REAL / f"{name}.sigmf-data", "<i2").astype(np.float32) / 32768
    return (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)


def test_am_on_real_wwv_recovers_the_broadcast_tone_and_time_code():
    # Clip centre 10.001 MHz: the WWV carrier is at -1000 Hz. AM audio carries the 600 Hz tone WWV
    # sent that minute and the 100 Hz time-code subcarrier.
    a = settled(chain(real_cf32("airspyhf_wwv10_48k"), 48000, -1000, "am"), 0.1)
    f, p = psd(a)
    med = np.median(p[(f > 100) & (f < 5000)])
    for hz in (600, 100):
        k = np.argmin(np.abs(f - hz))
        assert abs(f[k - 3:k + 4][np.argmax(p[k - 3:k + 4])] - hz) < 1.0
        assert 10 * np.log10(p[k - 3:k + 4].max() / med) > 20, hz


def test_usb_on_real_ft8_matches_an_independent_numpy_demodulator():
    x = real_cf32("airspyhf_ft8_40m_48k")
    a = chain(x, 48000, -1500, "usb", "--bw", 2700)[AUDIO:13 * AUDIO]     # dial 7.074 MHz, 300-3000 Hz
    # numpy reference: dial to 0 Hz, ideal 300-3000 Hz complex band-pass, real part, 48k -> 12k
    n = np.arange(len(x))
    y = x * np.exp(2j * np.pi * 1500 * n / 48000)
    Y = np.fft.fft(y)
    fy = np.fft.fftfreq(len(y), 1 / 48000)
    Y[(fy < 300) | (fy > 3000)] = 0
    ref = np.fft.ifft(Y).real[::4][AUDIO:13 * AUDIO]
    f, pa = psd(a)
    _, pr = psd(ref)
    band = (f > 400) & (f < 2900)
    bins = 50
    la = np.log10(pa[band][: band.sum() // bins * bins].reshape(-1, bins).mean(1))
    lr = np.log10(pr[band][: band.sum() // bins * bins].reshape(-1, bins).mean(1))
    assert np.corrcoef(la, lr)[0, 1] > 0.95
    above = (f > 3300) & (f < 5500)
    assert 10 * np.log10(pa[above].mean() / np.median(pa[band])) < -60     # nothing above the passband
    decodes = [ln.split() for ln in (REAL / "airspyhf_ft8_40m_48k.decodes.txt").read_text().splitlines()
               if ln and not ln.startswith("#")]
    med = np.median(pa[band])
    strong = [int(d[2]) for d in decodes if float(d[0]) >= 15 and 400 < int(d[2]) < 2850]
    excess = [10 * np.log10(pa[(f >= hz) & (f <= hz + 50)].mean() / med) for hz in strong]
    assert len(strong) >= 6 and min(excess) > 2 and np.median(excess) > 6, excess


# --------------------------------------------------------------------------- benchmark

def test_bench_runs_and_reports_every_m1_size_and_mode():
    out = [json.loads(ln) for ln in dsp("bench", "--seconds", 1.0, timeout=120).decode().splitlines()]
    ffts = {(e["n"], e["rate_hz"]) for e in out if e["event"] == "bench_fft"}
    assert {(n, r) for n in (2048, 4096) for r in (192000, 384000, 912000)} <= ffts
    chains = {(e["mode"], e["rate_hz"]) for e in out if e["event"] == "bench_chain"}
    assert {(m, r) for m in ("am", "usb", "lsb", "cw", "nfm", "wfm") for r in (192000, 384000, 912000)} <= chains
    assert all(e["fft_us"] > 0 for e in out if e["event"] == "bench_fft")
