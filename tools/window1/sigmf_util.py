"""Small SigMF helpers for Window 1 tools (multi-capture aware, cf32/ci16)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

DTYPES = {"cf32_le": np.complex64, "ci16_le": np.int16}


def load_meta(base: Path) -> dict:
    return json.loads(Path(str(base) + ".sigmf-meta").read_text())


def load_samples(base: Path) -> np.ndarray:
    meta = load_meta(base)
    dt = meta["global"]["core:datatype"]
    raw = np.fromfile(str(base) + ".sigmf-data", dtype=DTYPES[dt])
    if dt == "ci16_le":
        raw = (raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)) / 32768.0
    return raw.astype(np.complex64)


def sha512_of(path: Path) -> str:
    h = hashlib.sha512()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def segments(meta: dict, n_samples: int) -> list[tuple[int, int, int]]:
    """(start, end, frequency_hz) for each capture segment."""
    caps = meta["captures"]
    out = []
    for i, c in enumerate(caps):
        end = caps[i + 1]["core:sample_start"] if i + 1 < len(caps) else n_samples
        out.append((int(c["core:sample_start"]), int(end), int(c["core:frequency"])))
    return out
