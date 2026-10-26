#!/usr/bin/env python3
"""Add core:sha512 to probe recordings and check them. Usage: finalize.py BASE [BASE...]"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sigmf_util import sha512_of  # noqa: E402


def finalize(base: Path) -> dict:
    meta_p, data_p = Path(str(base) + ".sigmf-meta"), Path(str(base) + ".sigmf-data")
    meta = json.loads(meta_p.read_text())
    g = meta["global"]
    assert g["core:datatype"] == "cf32_le", g["core:datatype"]
    size = data_p.stat().st_size
    if size % 8:
        raise SystemExit(f"{data_p}: size {size} is not a whole number of cf32 samples")
    n = size // 8
    starts = [c["core:sample_start"] for c in meta["captures"]]
    if starts[0] != 0 or starts != sorted(starts) or starts[-1] >= max(n, 1):
        raise SystemExit(f"{meta_p}: capture sample_starts {starts} invalid for {n} samples")
    g["core:sha512"] = sha512_of(data_p)
    meta_p.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    return {"recording": str(base), "samples": n, "seconds": n / g["core:sample_rate"],
            "captures": len(starts), "discontinuities": len(meta["annotations"]), "sha512": g["core:sha512"][:16]}


if __name__ == "__main__":
    for b in sys.argv[1:]:
        print(json.dumps(finalize(Path(b))))
