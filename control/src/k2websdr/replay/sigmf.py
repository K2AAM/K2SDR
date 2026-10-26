"""SigMF recording loader for IQ/real replay (SigMF v1.2.x).

The control service parses SigMF metadata here and hands the worker explicit
parameters (data path, format, integer rate, integer frequency origin,
discontinuities). The worker never parses JSON.

Accepted subset, deliberately strict:
- datatypes ci16_le, cf32_le, ri16_le, rf32_le (the four frame_v1 formats);
- exactly one capture segment starting at sample 0 (multi-capture recordings
  are refused with an explicit error rather than half-supported);
- core:sample_rate and core:frequency must be whole numbers of Hz. SigMF
  stores them as JSON numbers, so integral floats (768000.0) are converted
  exactly; anything else is rejected;
- complex data requires core:frequency (the center); real data uses
  k2websdr:freq_origin_hz if present, otherwise 0 Hz (first Nyquist zone);
- discontinuities are annotations labelled "discontinuity" with
  core:sample_count 0 and optional k2websdr:gap_samples. Any recording using
  them must declare the k2websdr extension as non-optional, because a reader
  that ignored them would misread the data as continuous.

Extension definition: fixtures/k2websdr.sigmf-ext.md.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..protocol.frame import SampleFormat

EXTENSION = "k2websdr"
EXTENSION_VERSION = "1.0.0"
DISCONTINUITY_LABEL = "discontinuity"

DATATYPES = {
    "ci16_le": SampleFormat.CI16,
    "cf32_le": SampleFormat.CF32,
    "ri16_le": SampleFormat.RI16,
    "rf32_le": SampleFormat.RF32,
}
FORMAT_DATATYPE = {v: k for k, v in DATATYPES.items()}

MAX_META_BYTES = 1 << 20


class SigMFError(ValueError):
    """A recording is malformed or outside the accepted subset."""


@dataclass(frozen=True, slots=True)
class Discontinuity:
    sample_index: int          # first sample after the break
    gap_samples: int | None    # None = gap length unknown


@dataclass(frozen=True, slots=True)
class Recording:
    meta_path: Path
    data_path: Path
    format: SampleFormat
    sample_rate_hz: int
    freq_origin_hz: int
    sample_count: int
    discontinuities: tuple[Discontinuity, ...]
    sha512: str | None
    description: str | None


def _whole_hz(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SigMFError(f"{field} must be a number")
    if isinstance(value, float):
        if not value.is_integer():
            raise SigMFError(f"{field}={value!r} is not a whole number of Hz")
        value = int(value)
    return value


def _int_field(obj: dict, key: str, where: str, *, minimum: int = 0) -> int:
    v = obj.get(key)
    if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
        raise SigMFError(f"{where}: {key} must be an integer >= {minimum}")
    return v


def _sha512(path: Path) -> str:
    h = hashlib.sha512()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load(meta_path: str | Path, *, require_sha512: bool = False) -> Recording:
    meta_path = Path(meta_path)
    if meta_path.suffix != ".sigmf-meta":
        raise SigMFError(f"{meta_path}: expected a .sigmf-meta file")
    data_path = meta_path.with_suffix(".sigmf-data")
    raw = meta_path.read_bytes()
    if len(raw) > MAX_META_BYTES:
        raise SigMFError(f"{meta_path}: metadata larger than {MAX_META_BYTES} bytes")
    try:
        meta = json.loads(raw)
    except ValueError as e:
        raise SigMFError(f"{meta_path}: invalid JSON: {e}") from None
    if not isinstance(meta, dict) or not {"global", "captures", "annotations"} <= meta.keys():
        raise SigMFError("top level must contain global, captures and annotations")
    g, captures, annotations = meta["global"], meta["captures"], meta["annotations"]
    if not isinstance(g, dict) or not isinstance(captures, list) or not isinstance(annotations, list):
        raise SigMFError("global must be an object; captures and annotations must be arrays")

    version = g.get("core:version")
    if not isinstance(version, str) or not version.startswith("1.2."):
        raise SigMFError(f"unsupported core:version {version!r}; expected 1.2.x")
    datatype = g.get("core:datatype")
    if datatype not in DATATYPES:
        raise SigMFError(f"unsupported core:datatype {datatype!r}; expected one of {sorted(DATATYPES)}")
    fmt = DATATYPES[datatype]
    if "core:sample_rate" not in g:
        raise SigMFError("core:sample_rate is required for replay")
    rate = _whole_hz(g["core:sample_rate"], "core:sample_rate")
    if not 0 < rate <= 0xFFFFFFFF:
        raise SigMFError(f"core:sample_rate {rate} outside 1..{0xFFFFFFFF}")

    ext_declared, ext_required = False, False
    for ext in g.get("core:extensions", []):
        if not isinstance(ext, dict) or set(ext) != {"name", "version", "optional"}:
            raise SigMFError("each core:extensions entry needs exactly name, version, optional")
        if not (isinstance(ext["name"], str) and isinstance(ext["version"], str)
                and isinstance(ext["optional"], bool)):
            raise SigMFError("core:extensions entry has wrong field types")
        if ext["name"] == EXTENSION:
            if ext["version"].split(".")[0] != EXTENSION_VERSION.split(".")[0]:
                raise SigMFError(f"unsupported {EXTENSION} extension version {ext['version']}")
            ext_declared, ext_required = True, ext["optional"] is False

    if len(captures) != 1:
        raise SigMFError(f"exactly one capture segment is supported, found {len(captures)}")
    cap = captures[0]
    if not isinstance(cap, dict) or cap.get("core:sample_start") != 0:
        raise SigMFError("the capture segment must start at core:sample_start 0")
    if fmt.is_complex:
        if "core:frequency" not in cap:
            raise SigMFError("complex recordings need core:frequency (center) in the capture")
        origin = _whole_hz(cap["core:frequency"], "core:frequency")
        if f"{EXTENSION}:freq_origin_hz" in cap:
            raise SigMFError("k2websdr:freq_origin_hz is only for real recordings")
    else:
        if "core:frequency" in cap:
            raise SigMFError("real recordings use k2websdr:freq_origin_hz, not core:frequency")
        origin = cap.get(f"{EXTENSION}:freq_origin_hz", 0)
        if origin != 0 and not ext_declared:
            raise SigMFError("k2websdr:freq_origin_hz used without declaring the k2websdr extension")
        origin = _whole_hz(origin, "k2websdr:freq_origin_hz")

    if not data_path.is_file():
        raise SigMFError(f"missing dataset {data_path.name}")
    size = data_path.stat().st_size
    bps = fmt.bytes_per_sample
    if size % bps:
        raise SigMFError(f"dataset size {size} is not a multiple of {bps} bytes ({datatype})")
    count = size // bps
    if count == 0:
        raise SigMFError("dataset is empty")

    sha = g.get("core:sha512")
    if sha is not None:
        if _sha512(data_path) != sha:
            raise SigMFError("core:sha512 does not match the dataset")
    elif require_sha512:
        raise SigMFError("core:sha512 is required for this recording")

    discs: list[Discontinuity] = []
    for i, ann in enumerate(annotations):
        if not isinstance(ann, dict):
            raise SigMFError(f"annotation {i} must be an object")
        if ann.get("core:label") != DISCONTINUITY_LABEL:
            if any(k.startswith(f"{EXTENSION}:") for k in ann):
                raise SigMFError(f"annotation {i}: k2websdr fields only valid on discontinuity annotations")
            continue  # other annotations are data for humans; no effect on replay
        where = f"annotation {i}"
        if not ext_required:
            raise SigMFError("discontinuity annotations require the k2websdr extension declared optional=false")
        start = _int_field(ann, "core:sample_start", where, minimum=1)
        if ann.get("core:sample_count") != 0:
            raise SigMFError(f"{where}: discontinuities must set core:sample_count 0")
        if start >= count:
            raise SigMFError(f"{where}: sample_start {start} beyond dataset ({count} samples)")
        gap = ann.get(f"{EXTENSION}:gap_samples")
        if gap is not None:
            gap = _int_field(ann, f"{EXTENSION}:gap_samples", where, minimum=1)
        if discs and start <= discs[-1].sample_index:
            raise SigMFError(f"{where}: discontinuities must be strictly increasing")
        discs.append(Discontinuity(start, gap))

    desc = g.get("core:description")
    return Recording(meta_path=meta_path, data_path=data_path, format=fmt, sample_rate_hz=rate,
                     freq_origin_hz=origin, sample_count=count, discontinuities=tuple(discs),
                     sha512=sha, description=desc if isinstance(desc, str) else None)
