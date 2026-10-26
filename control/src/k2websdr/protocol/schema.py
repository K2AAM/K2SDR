"""Generate protocol/schema/control-v1.json from the pydantic models.

    python -m k2websdr.protocol.schema --write ../protocol/schema/control-v1.json
    python -m k2websdr.protocol.schema --check ../protocol/schema/control-v1.json

Output is deterministic (sorted keys, fixed indent). The committed file is a
review artifact: a protocol change shows up as a schema diff in the PR, and
--check fails CI when the models and the committed schema disagree.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .models import CLIENT_ADAPTER, PROTOCOL_VERSION, SERVER_ADAPTER

REF_TEMPLATE = "#/$defs/{model}"


def _split(adapter: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    schema = adapter.json_schema(mode="validation", ref_template=REF_TEMPLATE)
    defs = schema.pop("$defs", {})
    return schema, defs


def build_schema() -> dict[str, Any]:
    client, client_defs = _split(CLIENT_ADAPTER)
    server, server_defs = _split(SERVER_ADAPTER)
    defs: dict[str, Any] = dict(client_defs)
    for name, body in server_defs.items():
        if name in defs and defs[name] != body:
            raise RuntimeError(f"conflicting schema definitions for {name}")
        defs[name] = body
    for name in ("ClientMessage", "ServerMessage"):
        if name in defs:
            raise RuntimeError(f"model name collides with reserved root {name}")
    defs["ClientMessage"] = client
    defs["ServerMessage"] = server
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:k2websdr:control:v{PROTOCOL_VERSION}",
        "title": f"K2AAM WebSDR control protocol v{PROTOCOL_VERSION}",
        "description": "Generated from k2websdr.protocol.models. Do not edit by hand.",
        "$defs": defs,
        "oneOf": [{"$ref": "#/$defs/ClientMessage"}, {"$ref": "#/$defs/ServerMessage"}],
    }


def render() -> str:
    return json.dumps(build_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", type=Path)
    g.add_argument("--check", type=Path)
    a = p.parse_args(argv)
    text = render()
    if a.write:
        a.write.write_text(text, encoding="utf-8")
        print(f"wrote {a.write}")
        return 0
    current = a.check.read_text(encoding="utf-8") if a.check.exists() else ""
    if current != text:
        print(f"{a.check} is stale; run: make schema", file=sys.stderr)
        return 1
    print(f"{a.check} is current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
