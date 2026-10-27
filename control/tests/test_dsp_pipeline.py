"""Worker DSP (worker/src/dsp): spectrum, listener channels, AGC — M1 design §3.2 and §4.

Drives the worker's own classes through build/worker/ksdr-dsp-run. Synthetic signals give
exact checks; the real Airspy HF+ clips (fixtures/real/airspyhf) give the golden checks from
the design note: WWV carrier in the expected bin, AM audio with WWV's tone and time code,
FT8 audio with energy at the decoded offsets. Outputs are compared by property, not by hash:
liquid's SIMD path depends on the build host, so bit-exact outputs can differ across machines.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
RUN = Path(os.environ.get("K2_DSP_RUN_BIN", ROOT / "build/worker/ksdr-dsp-run"))
REAL = ROOT / "fixtures/real/airspyhf"
A = 12000                     # audio rate
FLOOR, STEP = -127.5, 0.5     # spectrum quantization


def run(*args, data: bytes = b"", ok=True, timeout=120):
    r = subprocess.run([str(RUN), *map(str, args)], input=data, capture_output=True, timeout=timeout)
    if ok:
        assert r.returncode == 0, r.stderr.decode()
    return r


def cf32(x) -> bytes:
    return np.asarray(x, np.complex64).tobytes()


def tone(rate, seconds, f, amp=1.0):
    n = np.arange(int(rate * seconds))
    return amp * np.exp(2j * np.pi * f * n / rate)


# ------------------------------------------------------------------------------ spectrum

def spectrum(x, rate, n=4096, fps=10, block=4096, fmt="cf32", raw=None):
    out = run("spectrum", "--format", fmt, "--rate", rate, "--fft", n, "--fps", fps, "--block", block,
              data=raw if raw is not None else cf32(x)).stdout
    size = 12 + n
    frames = []
    for i in range(0, len(out), size):
        end = int(np.frombuffer(out[i:i + 8], np.uint64)[0])
        blocks = int(np.frombuffer(out[i + 8:i + 12], np.uint32)[0])
        frames.append((end, blocks, np.frombuffer(out[i + 12:i + size], np.uint8)))
    return frames


def db(q):
    return FLOOR + STEP * q.astype(float)


@pytest.mark.parametrize("level_db", [0, -20, -60])
def test_spectrum_tone_in_its_bin_at_its_level(level_db):
    rate, n, k = 384000, 4096, 500
    x = tone(rate, 1.0, k * rate / n, 10 ** (level_db / 20))
    frames = spectrum(x, rate, n)
    _, _, bins = frames[-1]
    assert int(np.argmax(bins)) == n // 2 + k                         # FFT-shifted: bin 0 = -rate/2
    assert abs(db(bins).max() - level_db) <= 0.5                      # 0 dBFS = full-scale tone


def test_spectrum_negative_frequencies_and_white_noise_floor():
    rate, n = 192000, 2048
    rng = np.random.default_rng(3)
    sigma2 = 1e-4                                                     # E|x|^2
    noise = np.sqrt(sigma2 / 2) * (rng.standard_normal(rate) + 1j * rng.standard_normal(rate))
    k = -300
    frames = spectrum(noise + tone(rate, 1.0, k * rate / n, 0.1), rate, n)
    _, _, bins = frames[-1]
    assert int(np.argmax(bins)) == n // 2 + k
    w = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n) / n)
    expect = 10 * np.log10(sigma2 * np.sum(w ** 2) / np.sum(w) ** 2)  # per-bin noise, tone-referenced
    floor = np.median(db(bins))
    assert abs(floor - expect) < 1.5, (floor, expect)                 # median of averaged chi^2 bins


@pytest.mark.parametrize("n", [16, 64, 2048])
def test_spectrum_matches_an_independent_numpy_reference(n):
    """One block, no averaging: bins equal a numpy periodic-Hann FFT + fftshift, quantised the same way."""
    rng = np.random.default_rng(n)
    rate = 48000
    x = (0.3 * (rng.standard_normal(3 * n) + 1j * rng.standard_normal(3 * n))).astype(np.complex64)
    end, blocks, bins = spectrum(x, rate, n, fps=rate // n, block=n)[0]
    w = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n) / n)
    seg = x[end - n:end].astype(np.complex128)                       # the block that frame used
    p = np.abs(np.fft.fftshift(np.fft.fft(seg * w))) ** 2 / np.sum(w) ** 2
    ref = np.clip(np.round((10 * np.log10(p + 1e-30) - FLOOR) / STEP), 0, 255)
    assert blocks == 1
    assert np.abs(bins.astype(int) - ref).max() <= 1                  # float32 vs float64 rounding


@pytest.mark.parametrize("n", [15, 17, 1000, 1023, 3072, 131072])
def test_spectrum_rejects_sizes_that_are_not_powers_of_two_in_range(n):
    r = run("spectrum", "--format", "cf32", "--rate", 48000, "--fft", n, "--fps", 10, data=b"", ok=False)
    assert r.returncode == 2 and b"bad spectrum config" in r.stderr


def test_spectrum_frame_cadence_is_exact_and_blocks_average():
    rate, n, fps = 912000, 4096, 15
    frames = spectrum(tone(rate, 2.0, 1000, 0.01), rate, n, fps=fps)
    assert len(frames) == 2 * fps
    assert [f[0] for f in frames] == [k * rate // fps for k in range(1, 2 * fps + 1)]
    assert all(f[1] == (rate // fps) // n for f in frames)            # 14 blocks averaged per frame


def test_spectrum_averaging_smooths_the_noise():
    rng = np.random.default_rng(9)
    rate, n = 384000, 1024
    x = 0.01 * (rng.standard_normal(rate) + 1j * rng.standard_normal(rate))
    one = spectrum(x, rate, n, fps=375)                               # period 1024 -> 1 block
    many = spectrum(x, rate, n, fps=10)                               # 37 blocks
    assert one[-1][1] == 1 and many[-1][1] == 37
    assert np.std(db(many[-1][2])) < 0.35 * np.std(db(one[-1][2]))


def test_spectrum_output_does_not_depend_on_input_chunking():
    rng = np.random.default_rng(1)
    x = 0.05 * (rng.standard_normal(200_000) + 1j * rng.standard_normal(200_000))
    a = spectrum(x, 192000, 2048, fps=20, block=4096)
    b = spectrum(x, 192000, 2048, fps=20, block=777)
    assert [(e, k, q.tobytes()) for e, k, q in a] == [(e, k, q.tobytes()) for e, k, q in b]


def test_spectrum_of_real_wwv_puts_the_carrier_in_the_expected_bin():
    raw = (REAL / "airspyhf_wwv10_48k.sigmf-data").read_bytes()
    n = 4096
    frames = spectrum(None, 48000, n, fps=10, fmt="ci16", raw=raw)
    acc = np.mean([db(f[2]) for f in frames], axis=0)
    expect = n // 2 + round(-1000 * n / 48000)                        # 10.000 MHz in a 10.001 MHz clip
    assert abs(int(np.argmax(acc)) - expect) <= 1
    assert acc.max() - np.median(acc) > 30


# ------------------------------------------------------------------------------- channels

def channel(x, rate, offset, mode, *extra, block=4096):
    r = run("channel", "--format", "cf32", "--rate", rate, "--offset", offset, "--mode", mode, "--block", block,
            *extra, data=cf32(x))
    return np.frombuffer(r.stdout, np.int16).astype(float) / 32768, json.loads(r.stderr.decode().strip().splitlines()[-1])


def psd(a):
    p = np.abs(np.fft.rfft(a * np.hanning(len(a)))) ** 2
    return np.fft.rfftfreq(len(a), 1 / A), p


def peak_hz(a):
    f, p = psd(a)
    p[f < 50] = 0
    return float(f[np.argmax(p)])


def rms_db(a):
    return 10 * np.log10(np.mean(np.square(a)) + 1e-30)


def tail(a, s=0.3):
    return a[int(s * A):]


def test_channel_reports_integer_defaults_and_exact_audio_rate():
    a, st = channel(tone(384000, 2.0, 10_000 + 1000, 0.1), 384000, 10_000, "usb")
    assert (st["pass_lo_hz"], st["pass_hi_hz"], st["channel_rate_hz"]) == (300, 2700, 12000)
    assert st["in_samples"] == 768000 and abs(st["out_samples"] - 2 * A) <= 64 and st["clipped"] == 0
    assert abs(peak_hz(tail(a)) - 1000) < 3


@pytest.mark.parametrize("mode, f_off, heard", [("usb", 1500, 1500), ("lsb", -1500, 1500), ("cw", 0, 700),
                                                ("am", 0, None)])
def test_each_mode_hears_its_signal(mode, f_off, heard):
    rate, off = 192000, -20_000
    if mode == "am":
        n = np.arange(rate)
        x = 0.2 * (1 + 0.6 * np.cos(2 * np.pi * 800 * n / rate)) * np.exp(2j * np.pi * off * n / rate)
        heard = 800
    else:
        x = tone(rate, 1.0, off + f_off, 0.05)
    a, _ = channel(x, rate, off, mode)
    assert abs(peak_hz(tail(a)) - heard) < 3


def test_sideband_and_out_of_channel_rejection():
    rate, off = 384000, 0
    usb_wanted, _ = channel(tone(rate, 1.0, off + 1200, 0.1), rate, off, "usb", "--agc", "off")
    usb_image, _ = channel(tone(rate, 1.0, off - 1200, 0.1), rate, off, "usb", "--agc", "off")
    far, _ = channel(tone(rate, 1.0, off + 30_000, 0.1), rate, off, "usb", "--agc", "off")
    assert rms_db(tail(usb_wanted)) - rms_db(tail(usb_image)) > 55
    assert rms_db(tail(usb_wanted)) - rms_db(tail(far)) > 60


def test_cw_pitch_follows_bfo_and_narrow_filter_rejects_neighbours():
    rate, off = 192000, 5000
    a, _ = channel(tone(rate, 1.0, off, 0.05), rate, off, "cw", "--bfo", 600)
    assert abs(peak_hz(tail(a)) - 600) < 3
    near, _ = channel(tone(rate, 1.0, off + 800, 0.05), rate, off, "cw", "--agc", "off")   # 800 Hz away
    on, _ = channel(tone(rate, 1.0, off, 0.05), rate, off, "cw", "--agc", "off")
    assert rms_db(tail(on)) - rms_db(tail(near)) > 40


def fm(rate, seconds, carrier, fmod, dev, amp=0.3):
    n = np.arange(int(rate * seconds))
    return amp * np.exp(1j * (2 * np.pi * carrier * n / rate + (dev / fmod) * np.sin(2 * np.pi * fmod * n / rate)))


def test_nfm_and_wfm_demodulate():
    a, st = channel(fm(384000, 1.0, 40_000, 1000, 3000), 384000, 40_000, "nfm", "--agc", "off")
    assert st["channel_rate_hz"] == 24000 and abs(peak_hz(tail(a)) - 1000) < 3
    assert abs(np.std(tail(a)) * np.sqrt(2) - 3000 / 5000) < 0.05      # full scale = 5 kHz deviation
    w1, st = channel(fm(912000, 1.0, 100_000, 1000, 75_000), 912000, 100_000, "wfm", "--agc", "off")
    w3, _ = channel(fm(912000, 1.0, 100_000, 3000, 75_000), 912000, 100_000, "wfm", "--agc", "off")
    assert st["channel_rate_hz"] == 192000 and abs(peak_hz(tail(w3)) - 3000) < 3
    h = lambda f: 1 / np.sqrt(1 + (2 * np.pi * f * 75e-6) ** 2)       # noqa: E731  75 us de-emphasis
    assert abs(20 * np.log10(np.std(tail(w3)) / np.std(tail(w1))) - 20 * np.log10(h(3000) / h(1000))) < 0.3


def test_s_meter_reads_dbfs_inside_the_passband_only(tmp_path):
    rate, off = 384000, 12_000
    for level in (0, -20, -50):
        _, st = channel(tone(rate, 1.0, off + 1000, 10 ** (level / 20)), rate, off, "usb")
        assert abs(st["level_dbfs"] - level) < 0.3, (level, st)
    _, st = channel(tone(rate, 1.0, off - 1000, 1.0), rate, off, "usb")    # wrong sideband
    assert st["level_dbfs"] < -60
    levels = tmp_path / "lv.jsonl"
    channel(tone(rate, 1.0, off + 1000, 0.1), rate, off, "usb", "--levels", levels)
    rows = [json.loads(x) for x in levels.read_text().splitlines()]
    assert len(rows) >= 9 and abs(rows[-1]["level_dbfs"] + 20) < 0.3


@pytest.mark.parametrize("args, reason", [
    (["--rate", 8000, "--mode", "usb"], "below the 12000 Hz"),
    (["--rate", 384000, "--mode", "wfm", "--offset", 150_000], "outside the capture span"),
    (["--rate", 192000, "--mode", "usb", "--lo", 300, "--hi", 9000], "exceeds the channel bandwidth"),
    (["--rate", 192000, "--mode", "usb", "--lo", 2000, "--hi", 1000], "low edge must be below"),
    (["--rate", 192000, "--mode", "cw", "--bfo", 50], "BFO"),
])
def test_invalid_channel_configs_are_refused_with_a_reason(args, reason):
    r = run("channel", "--format", "cf32", *args, ok=False)
    assert r.returncode == 2 and reason in json.loads(r.stderr.decode())["error"]


def test_retune_is_phase_continuous_and_moves_the_channel():
    rate, off1, off2 = 192000, 10_000, 30_000
    n = np.arange(rate)
    x = 0.05 * (np.exp(2j * np.pi * (off1 + 1000) * n / rate) + np.exp(2j * np.pi * (off2 + 2000) * n / rate))
    a, _ = channel(x, rate, off1, "usb", "--agc", "off", "--manual-gain", 20, "--retune", f"{rate // 2}:{off2}")
    first, second = a[int(0.1 * A):int(0.45 * A)], a[int(0.6 * A):]
    assert abs(peak_hz(first) - 1000) < 3 and abs(peak_hz(second) - 2000) < 3
    # No click: no sample-to-sample jump larger than the largest a 2 kHz tone of this amplitude makes.
    amp = np.max(np.abs(second))
    assert np.max(np.abs(np.diff(a[int(0.4 * A):int(0.6 * A)]))) < 2.5 * amp * 2 * np.pi * 2000 / A


def test_channel_output_is_deterministic_and_independent_of_chunking():
    rng = np.random.default_rng(5)
    x = 0.02 * (rng.standard_normal(300_000) + 1j * rng.standard_normal(300_000)) + tone(384000, 300_000 / 384000, 7000, 0.05)
    a1, _ = channel(x, 384000, 6000, "usb", block=4096)
    a2, _ = channel(x, 384000, 6000, "usb", block=4096)
    a3, _ = channel(x, 384000, 6000, "usb", block=1000)
    assert np.array_equal(a1, a2) and np.array_equal(a1, a3)


# ---------------------------------------------------------------------------------- AGC

def agc(x, mode, *extra):
    r = run("agc", "--mode", mode, *extra, data=np.asarray(x, np.float32).tobytes())
    return np.frombuffer(r.stdout, np.float32), json.loads(r.stderr.decode())


def _lag(x, y, max_lag=200):
    """Lag (samples) at which y best matches x."""
    return max(range(max_lag), key=lambda d: float(np.dot(x[: len(x) - d], y[d:])))


@pytest.mark.parametrize("lookahead_ms, latency", [(0, 0), (None, 60), (2.5, 30)])
def test_agc_delay_equals_its_reported_latency(lookahead_ms, latency):
    """White noise in, gain settled: the output is the input delayed by exactly latency_samples."""
    x = np.random.default_rng(5).uniform(-0.05, 0.05, 2 * A)
    extra = [] if lookahead_ms is None else ["--lookahead-ms", lookahead_ms]
    y, info = agc(x, "medium", *extra)
    assert info["latency_samples"] == latency
    tail = slice(A, None)                                              # past the initial attack
    assert _lag(x[tail], y[tail]) == latency
    if latency == 0:                                                   # same sample, same sign, every time
        assert np.all(np.sign(y[tail]) == np.sign(x[tail]))


def step_signal(up: bool, seconds=12):
    n = np.arange(seconds * A)
    x = np.sin(2 * np.pi * 500 * n / A)
    half = len(x) // 2
    if up:
        x[:half] *= 0.01
    else:
        x[half:] *= 0.01
    return x, half


def peak_db(a):
    return 20 * np.log10(np.max(np.abs(a)) + 1e-30)


@pytest.mark.parametrize("mode", ["fast", "medium", "slow"])
def test_agc_does_not_blast_on_a_sudden_40db_signal(mode):
    x, half = step_signal(up=True)
    y, meta = agc(x, mode)
    assert meta["latency_samples"] == 60                               # 5 ms look-ahead at 12 kS/s
    assert peak_db(y[half - A:half]) == pytest.approx(-6.0, abs=0.3)  # target reached while quiet
    assert peak_db(y[half:]) < -6.0 + 1.0                              # liquid's loop AGC: +27..+40 dB here
    assert peak_db(y[-A:]) == pytest.approx(-6.0, abs=0.3)


def recovery_s(y, half):
    """After a 40 dB drop: time until 10 ms peaks are back within 3 dB of the target."""
    w = A // 100
    start = half + 60                                                  # the drop reaches the output 5 ms later
    for i in range(start, len(y) - w, w):
        if peak_db(y[i:i + w]) > -9.0:
            return (i - start) / A
    return float("inf")


def test_agc_recovery_follows_hang_and_decay_per_mode():
    x, half = step_signal(up=False)
    t = {m: recovery_s(agc(x, m)[0], half) for m in ("fast", "medium", "slow")}
    # The envelope decays from 1 towards the new peak 0.01: env = 0.01 + 0.99 exp(-t/decay). Within
    # 3 dB of target once env <= 0.01*sqrt(2): t = hang + decay * ln(0.99 / 0.00414) = hang + 5.48 decay
    # -> fast 0.274 s, medium 1.47 s, slow 4.61 s.
    expect = {"fast": 0.0 + 5.48 * 0.050, "medium": 0.1 + 5.48 * 0.250, "slow": 0.5 + 5.48 * 0.750}
    assert all(abs(t[m] - expect[m]) < 0.03 + 0.03 * expect[m] for m in t), (t, expect)


def test_agc_off_is_an_exact_manual_gain_and_int16_clips_are_counted():
    x = 0.01 * np.sin(2 * np.pi * 440 * np.arange(A) / A)
    y, meta = agc(x, "off", "--manual-gain", 20)
    assert meta["latency_samples"] == 0 and np.allclose(y, 10 * x, rtol=1e-5, atol=1e-7)
    _, st = channel(tone(192000, 0.5, 1000, 0.5), 192000, 0, "usb", "--agc", "off", "--manual-gain", 40)
    assert st["clipped"] > 0


def test_agc_gain_is_capped_for_a_signal_below_the_noise_floor():
    x = 1e-6 * np.sin(2 * np.pi * 500 * np.arange(2 * A) / A)          # -120 dBFS
    y, _ = agc(x, "fast")
    assert peak_db(y[A:]) == pytest.approx(-120 + 70, abs=0.5)         # max gain 70 dB


# ----------------------------------------------------------------------- real recordings

def real(name):
    raw = np.fromfile(REAL / f"{name}.sigmf-data", "<i2").astype(np.float32) / 32768
    return raw[0::2] + 1j * raw[1::2]


def test_golden_am_on_real_wwv():
    a, st = channel(real("airspyhf_wwv10_48k"), 48000, -1000, "am")
    f, p = psd(tail(a))
    med = np.median(p[(f > 100) & (f < 4000)])
    for hz in (600, 100):                                              # WWV tone, time-code subcarrier
        band = np.abs(f - hz) < 2
        assert 10 * np.log10(p[band].max() / med) > 20, hz
    assert st["level_dbfs"] < 0 and st["clipped"] == 0


def test_golden_usb_on_real_ft8_has_energy_at_the_decoded_offsets():
    x = real("airspyhf_ft8_40m_48k")
    a, st = channel(x, 48000, -1500, "usb", "--lo", 200, "--hi", 3000)    # dial 7.074 MHz
    a = a[A:13 * A]
    f, p = psd(a)
    band = (f > 400) & (f < 2900)
    med = np.median(p[band])
    decodes = [ln.split() for ln in (REAL / "airspyhf_ft8_40m_48k.decodes.txt").read_text().splitlines()
               if ln and not ln.startswith("#")]
    strong = [int(d[2]) for d in decodes if float(d[0]) >= 15 and 400 < int(d[2]) < 2850]
    excess = [10 * np.log10(p[(f >= hz) & (f <= hz + 50)].mean() / med) for hz in strong]
    assert len(strong) >= 6 and min(excess) > 2 and np.median(excess) > 6, excess
    above = (f > 3300) & (f < 5500)
    assert 10 * np.log10(p[above].mean() / med) < -55
    assert st["clipped"] == 0
