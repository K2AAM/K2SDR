#!/usr/bin/env python3
"""Import the baseline mode/service ledger from the outline appendix.

    python3 tools/import_ledger.py docs/outline/WebSDR_Project_Outline-v0.1.md ledger/modes.v1.json

Data only: every entry starts not_started / unavailable. Nothing here implies
an adapter exists. Output is deterministic.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

EXPECTED = 74
ROW = re.compile(r"^\|\s*`([a-z0-9-]+)`\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|\s*$")
KINDS = {"Receiver/decoder mode": "mode", "Background service": "service"}
UPSTREAM = {
    "repo": "K2AAM/openwebrx-k2aam",
    "path": "owrx/modes.py",
    "commit": "86915038202fb273c13274420c7e882b32b19a0a",
    "blob": "ad85d5c7de124f0c125e5b045ada283a97acc1c9",
}


def build(src: Path) -> dict:
    raw = src.read_bytes()
    _, found, appendix = raw.decode("utf-8").partition("## Appendix")
    if not found:
        raise SystemExit("appendix heading not found")
    entries = []
    for line in appendix.splitlines():
        mt = ROW.match(line)
        if not mt:
            continue
        mid, name, kind, feat = mt.groups()
        if kind not in KINDS:
            raise SystemExit(f"{mid}: unknown kind {kind!r}")
        entries.append({
            "id": mid, "name": name, "kind": KINDS[kind], "required_feature": feat or None,
            "family": None, "status": "not_started", "adapter": None, "engine_version": None,
            "supported_inputs": [], "reference_sample": None, "test_result": None,
            "live_validation": "none", "limitations": [],
        })
    ids = [e["id"] for e in entries]
    if len(ids) != EXPECTED or len(set(ids)) != len(ids):
        raise SystemExit(f"expected {EXPECTED} unique ids, got {len(ids)} ({len(set(ids))} unique)")
    return {
        "ledger_version": 1,
        "provenance": {
            "imported_from": "docs/outline/WebSDR_Project_Outline-v0.1.md",
            "outline_sha256": hashlib.sha256(raw).hexdigest(),
            "upstream": UPSTREAM,
            "note": "Appendix is derived from upstream with default WSJT-X/JS8Call requirements made "
                    "explicit. Upstream comparison: ledger/PROVENANCE.md.",
        },
        "entries": entries,
    }


def render(src: Path) -> str:
    return json.dumps(build(src), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 3 and not (len(argv) == 4 and argv[1] == "--check"):
        print(__doc__, file=sys.stderr)
        return 64
    if argv[1] == "--check":
        src, dst = Path(argv[2]), Path(argv[3])
        if not dst.exists() or dst.read_text("utf-8") != render(src):
            print(f"{dst} differs from the outline appendix; run: make ledger", file=sys.stderr)
            return 1
        print(f"{dst} is current")
        return 0
    src, dst = Path(argv[1]), Path(argv[2])
    dst.write_text(render(src), encoding="utf-8")
    print(f"wrote {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
