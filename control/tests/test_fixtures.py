"""A8 (fixture half): committed synthetic fixtures are reproducible, load correctly,
and contain exactly the signals their metadata claims.

The worker half of A8 (replay emits KSDR_F_DISCONTINUITY at the annotated
index) is tested with the worker in block 3.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from k2websdr.protocol.frame import SampleFormat
from k2websdr.replay import sigmf
from k2websdr.replay.sigmf import Discontinuity

ROOT = Path(__file__).resolve().parents[2]
IQ = ROOT / "fixtures" / "iq"


def _generator():
    spec = importlib.util.spec_from_file_location("make_synthetic", ROOT / "fixtures/gen/make_synthetic.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["make_synthetic"] = mod
    spec.loader.exec_module(mod)
    return mod


GEN = _generator()


def test_committed_fixtures_are_reproducible():
    assert GEN.main(["--check", str(IQ)]) == 0


def test_every_committed_fixture_is_generated():
    on_disk = {p.name.split(".")[0] for p in IQ.glob("*.sigmf-*")}
    assert on_disk == {fx.name for fx in GEN.FIXTURES}


def test_prng_reference_values():
    # Pins the integer PRNG; a change here would silently change every fixture.
    r = GEN.XorShift64Star(1)
    assert [r.next() for _ in range(3)] == [
        5180492295206395165, 12380297144915551517, 13389498078930870103]  # cross-checked against a C reference


def load(name: str) -> sigmf.Recording:
    return sigmf.load(IQ / f"{name}.sigmf-meta", require_sha512=True)


def samples(rec: sigmf.Recording) -> np.ndarray:
    raw = np.fromfile(rec.data_path, dtype="<i2").astype(np.float64)
    return raw[0::2] + 1j * raw[1::2] if rec.format.is_complex else raw


def test_two_tone_metadata_and_bins():
    rec = load("two_tone_ci16")
    assert (rec.format, rec.sample_rate_hz, rec.freq_origin_hz, rec.sample_count) == \
        (SampleFormat.CI16, 48_000, 7_074_000, 12_000)
    assert rec.discontinuities == ()
    x = samples(rec)
    mag = np.abs(np.fft.fft(x)) / len(x)
    bin_hz = rec.sample_rate_hz / len(x)
    top2 = sorted(np.argsort(mag)[-2:].tolist())
    # +1500 Hz -> bin 375; -3000 Hz -> bin N-750
    assert bin_hz == 4.0
    assert top2 == [375, len(x) - 750]
    assert mag[375] == pytest.approx(8_000, rel=0.01)
    assert mag[len(x) - 750] == pytest.approx(4_000, rel=0.01)
    # Absolute frequencies are integer Hz end to end.
    assert rec.freq_origin_hz + 1_500 == 7_075_500


def test_real_tone_metadata_and_bin():
    rec = load("tone_ri16")
    assert (rec.format, rec.sample_rate_hz, rec.freq_origin_hz, rec.sample_count) == \
        (SampleFormat.RI16, 64_000, 0, 16_384)
    x = samples(rec)
    mag = np.abs(np.fft.rfft(x)) / len(x)
    peak = int(np.argmax(mag))
    assert peak * rec.sample_rate_hz / len(x) == 10_000.0
    assert peak == 2560
    assert 2 * mag[peak] == pytest.approx(10_000, rel=0.01)   # real tone splits power across ±f


def _phase_jump(d: np.ndarray, at: int, steady: float) -> float:
    """Extra phase (0..2pi) across the boundary into sample `at`, beyond the steady step."""
    return float((d[at - 1] - steady) % (2 * np.pi))


def test_gap_fixture_discontinuities_and_phase():
    rec = load("gap_ci16")
    assert rec.discontinuities == (Discontinuity(4_800, 2_412), Discontinuity(7_200, None))
    x = samples(rec)
    steady = 2 * np.pi * 1_000 / 48_000
    d = np.angle(x[1:] * np.conj(x[:-1]))
    # Away from the breaks the tone advances by exactly one steady step per sample.
    mask = np.ones_like(d, dtype=bool)
    mask[[4_799, 7_199]] = False
    assert np.median(np.abs(d[mask] - steady)) < 0.02
    # Known gap: 2412 lost samples = 50.25 cycles -> +pi/2 extra phase.
    assert _phase_jump(d, 4_800, steady) == pytest.approx(np.pi / 2, abs=0.05)
    # Unknown gap rendered as 1000 lost samples = 20.8333 cycles -> 0.8333 * 2pi.
    assert _phase_jump(d, 7_200, steady) == pytest.approx((1_000 * 1_000 / 48_000 % 1) * 2 * np.pi, abs=0.05)


# ------------------------------------------------------------- loader rejects

def write_rec(tmp_path: Path, meta: dict, data: bytes = b"\0" * 64, name: str = "r") -> Path:
    (tmp_path / f"{name}.sigmf-data").write_bytes(data)
    p = tmp_path / f"{name}.sigmf-meta"
    p.write_text(json.dumps(meta))
    return p


def base_meta(**g) -> dict:
    glob = {"core:datatype": "ci16_le", "core:sample_rate": 48000, "core:version": "1.2.6"}
    glob.update(g)
    return {"global": glob, "captures": [{"core:sample_start": 0, "core:frequency": 7074000}],
            "annotations": []}


def test_minimal_valid(tmp_path):
    rec = sigmf.load(write_rec(tmp_path, base_meta()))
    assert rec.sample_count == 16 and rec.freq_origin_hz == 7_074_000


def test_integral_float_rate_and_frequency_are_converted_exactly(tmp_path):
    m = base_meta(**{"core:sample_rate": 48000.0})
    m["captures"][0]["core:frequency"] = 7074000.0
    rec = sigmf.load(write_rec(tmp_path, m))
    assert rec.sample_rate_hz == 48_000 and type(rec.sample_rate_hz) is int
    assert rec.freq_origin_hz == 7_074_000 and type(rec.freq_origin_hz) is int


def _mut(fn):
    m = base_meta()
    fn(m)
    return m


@pytest.mark.parametrize("meta,data", [
    (base_meta(**{"core:version": "1.0.0"}), None),
    (base_meta(**{"core:datatype": "cu8"}), None),
    (base_meta(**{"core:datatype": "ci16_be"}), None),
    (base_meta(**{"core:sample_rate": 48000.5}), None),
    (base_meta(**{"core:sample_rate": True}), None),
    (base_meta(**{"core:sample_rate": 0}), None),
    (_mut(lambda m: m["global"].pop("core:sample_rate")), None),
    (_mut(lambda m: m["captures"][0].__setitem__("core:frequency", 7074000.25)), None),
    (_mut(lambda m: m["captures"][0].pop("core:frequency")), None),
    (_mut(lambda m: m["captures"].append({"core:sample_start": 8})), None),
    (_mut(lambda m: m["captures"][0].__setitem__("core:sample_start", 4)), None),
    (_mut(lambda m: m.pop("annotations")), None),
    (base_meta(**{"core:sha512": "0" * 128}), None),
    (base_meta(), b"\0" * 63),                    # not a whole number of ci16 samples
    (base_meta(), b""),
    (_mut(lambda m: m["annotations"].append(      # discontinuity without the extension
        {"core:sample_start": 4, "core:sample_count": 0, "core:label": "discontinuity"})), None),
])
def test_loader_rejects(tmp_path, meta, data):
    with pytest.raises(sigmf.SigMFError):
        sigmf.load(write_rec(tmp_path, meta, data if data is not None else b"\0" * 64))


def with_ext(optional: bool = False) -> dict:
    return base_meta(**{"core:extensions": [{"name": "k2websdr", "version": "1.0.0", "optional": optional}]})


@pytest.mark.parametrize("ann", [
    [{"core:sample_start": 4, "core:label": "discontinuity"}],                          # count omitted = to end
    [{"core:sample_start": 4, "core:sample_count": 1, "core:label": "discontinuity"}],
    [{"core:sample_start": 0, "core:sample_count": 0, "core:label": "discontinuity"}],
    [{"core:sample_start": 16, "core:sample_count": 0, "core:label": "discontinuity"}],
    [{"core:sample_start": 4, "core:sample_count": 0, "core:label": "discontinuity", "k2websdr:gap_samples": 0}],
    [{"core:sample_start": 8, "core:sample_count": 0, "core:label": "discontinuity"},
     {"core:sample_start": 8, "core:sample_count": 0, "core:label": "discontinuity"}],
    [{"core:sample_start": 4, "core:sample_count": 2, "core:label": "note", "k2websdr:gap_samples": 3}],
])
def test_discontinuity_rules(tmp_path, ann):
    m = with_ext()
    m["annotations"] = ann
    with pytest.raises(sigmf.SigMFError):
        sigmf.load(write_rec(tmp_path, m))


def test_discontinuities_need_non_optional_extension(tmp_path):
    ann = [{"core:sample_start": 4, "core:sample_count": 0, "core:label": "discontinuity"}]
    m = with_ext(optional=True)
    m["annotations"] = ann
    with pytest.raises(sigmf.SigMFError):
        sigmf.load(write_rec(tmp_path, m))
    m = with_ext(optional=False)
    m["annotations"] = ann
    assert sigmf.load(write_rec(tmp_path, m)).discontinuities == (Discontinuity(4, None),)


def test_other_annotations_are_ignored(tmp_path):
    m = base_meta()
    m["annotations"] = [{"core:sample_start": 2, "core:sample_count": 4, "core:label": "FT8?"}]
    assert sigmf.load(write_rec(tmp_path, m)).discontinuities == ()


def test_real_recording_origin_rules(tmp_path):
    m = base_meta(**{"core:datatype": "ri16_le"})
    m["captures"] = [{"core:sample_start": 0}]
    assert sigmf.load(write_rec(tmp_path, m)).freq_origin_hz == 0
    m["captures"][0]["core:frequency"] = 1
    with pytest.raises(sigmf.SigMFError):
        sigmf.load(write_rec(tmp_path, m))
    m["captures"] = [{"core:sample_start": 0, "k2websdr:freq_origin_hz": 32_000_000}]
    with pytest.raises(sigmf.SigMFError):          # extension not declared
        sigmf.load(write_rec(tmp_path, m))
    m["global"]["core:extensions"] = [{"name": "k2websdr", "version": "1.0.0", "optional": True}]
    assert sigmf.load(write_rec(tmp_path, m)).freq_origin_hz == 32_000_000


def test_require_sha512(tmp_path):
    with pytest.raises(sigmf.SigMFError):
        sigmf.load(write_rec(tmp_path, base_meta()), require_sha512=True)
