#!/usr/bin/env python3
"""Compare ledger/modes.v1.json with an upstream OpenWebRX+ owrx/modes.py.

    python3 tools/diff_ledger_upstream.py path/to/modes.py ledger/modes.v1.json

The upstream file is PARSED (ast), never executed or imported, and none of its
code is copied: only mode ids, names, constructor classes (for mode vs
service kind) and requirement lists are read from the `Modes.mappings` list. Commented-out entries are not
part of the AST and are therefore excluded. Prints a Markdown report.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from pathlib import Path

DEFAULT_REQS = {"WsjtMode": ["wsjt-x"], "Js8Mode": ["js8call"]}


def _const(node):
    return node.value if isinstance(node, ast.Constant) else None


def upstream_entries(src: str) -> list[dict]:
    tree = ast.parse(src)
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Modes"):
        for stmt in cls.body:
            if isinstance(stmt, ast.Assign) and any(getattr(t, "id", None) == "mappings" for t in stmt.targets):
                if not isinstance(stmt.value, ast.List):
                    raise SystemExit("Modes.mappings is not a list literal")
                out = []
                for el in stmt.value.elts:
                    if not isinstance(el, ast.Call) or len(el.args) < 2:
                        raise SystemExit(f"unexpected mappings element at line {el.lineno}")
                    ctor = getattr(el.func, "id", None) or getattr(el.func, "attr", "?")
                    kw = {k.arg: k.value for k in el.keywords}
                    reqs = None
                    if "requirements" in kw and isinstance(kw["requirements"], ast.List):
                        reqs = [_const(x) for x in kw["requirements"].elts]
                    out.append({"id": _const(el.args[0]), "name": _const(el.args[1]), "ctor": ctor,
                                "requirements": reqs, "line": el.lineno})
                return out
    raise SystemExit("class Modes with a mappings list not found")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 64
    upath, lpath = Path(argv[1]), Path(argv[2])
    usrc = upath.read_bytes()
    blob = hashlib.sha1(b"blob %d\0" % len(usrc) + usrc).hexdigest()
    up = upstream_entries(usrc.decode("utf-8"))
    ledger = json.loads(lpath.read_text("utf-8"))
    led = {e["id"]: e for e in ledger["entries"]}
    upd = {e["id"]: e for e in up}

    print(f"- Upstream file git blob: `{blob}` (expected `{ledger['provenance']['upstream']['blob']}`: "
          f"{'match' if blob == ledger['provenance']['upstream']['blob'] else 'MISMATCH'})")
    print(f"- Active entries in `Modes.mappings`: {len(up)}; ledger entries: {len(led)}")
    dup = sorted({e['id'] for e in up if [x['id'] for x in up].count(e['id']) > 1})
    print(f"- Duplicate upstream ids: {', '.join(dup) if dup else 'none'}")
    only_up = [i for i in upd if i not in led]
    only_led = [i for i in led if i not in upd]
    print(f"- In upstream but not ledger: {', '.join(only_up) if only_up else 'none'}")
    print(f"- In ledger but not upstream: {', '.join(only_led) if only_led else 'none'}")
    order_ok = [e["id"] for e in up] == [e["id"] for e in ledger["entries"]]
    print(f"- Same order: {'yes' if order_ok else 'no'}")
    diffs = []
    for i in (x for x in upd if x in led):
        u, l = upd[i], led[i]
        if u["name"] != l["name"]:
            diffs.append(f"`{i}` name: upstream {u['name']!r}, ledger {l['name']!r}")
        up_kind = "service" if u["ctor"] == "ServiceOnlyMode" else "mode"
        if up_kind != l["kind"]:
            diffs.append(f"`{i}` kind: upstream {up_kind} ({u['ctor']}), ledger {l['kind']}")
        reqs = u["requirements"] if u["requirements"] is not None else DEFAULT_REQS.get(u["ctor"])
        up_feat = None if not reqs else ",".join(reqs)
        if up_feat != l["required_feature"]:
            diffs.append(f"`{i}` requirement: upstream {up_feat!r} (via {u['ctor']} line {u['line']}), "
                         f"ledger {l['required_feature']!r}")
    print(f"- Field differences: {len(diffs)}")
    for d in diffs:
        print(f"  - {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
