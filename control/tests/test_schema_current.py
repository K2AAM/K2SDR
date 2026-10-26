"""A14: the committed JSON Schema matches what the models generate."""

from __future__ import annotations

from pathlib import Path

from k2websdr.protocol import schema

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "protocol/schema/control-v1.json"


def test_committed_schema_is_current():
    assert SCHEMA_PATH.exists(), "run: make schema"
    assert SCHEMA_PATH.read_text("utf-8") == schema.render(), "schema is stale; run: make schema"


def test_render_is_deterministic():
    assert schema.render() == schema.render()


def test_check_mode_reports_stale(tmp_path):
    stale = tmp_path / "s.json"
    stale.write_text("{}\n", "utf-8")
    assert schema.main(["--check", str(stale)]) == 1
    assert schema.main(["--write", str(stale)]) == 0
    assert schema.main(["--check", str(stale)]) == 0
