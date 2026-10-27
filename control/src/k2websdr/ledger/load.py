"""Load the mode ledger and decide availability with explicit reasons.

A mode is available only when (outline §2, §6):
  - an adapter is integrated and its tests pass (status "integrated"),
  - its required dependency has been detected on this host,
  - (later) the receiver's bandwidth and signal format support it.
Every unavailable mode carries human-readable reasons for the admin view.
Integrations come from the reviewed overlay ledger/integrations.v1.json (M1: k2dsp modes).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from ..protocol import models as m

LedgerStatus = Literal["not_started", "in_progress", "integrated"]


class LedgerEntry(m.Strict):
    id: m.ModeId
    name: Annotated[str, Field(max_length=64)]
    kind: Literal["mode", "service"]
    required_feature: Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,48}$")] | None
    family: Annotated[str, Field(max_length=48)] | None
    status: LedgerStatus
    adapter: Annotated[str, Field(max_length=64)] | None
    engine_version: Annotated[str, Field(max_length=64)] | None
    supported_inputs: list[Annotated[str, Field(max_length=64)]]
    reference_sample: Annotated[str, Field(max_length=256)] | None
    test_result: Annotated[str, Field(max_length=256)] | None
    live_validation: Literal["none", "recorded", "live"]
    limitations: list[Annotated[str, Field(max_length=256)]]


class Upstream(m.Strict):
    repo: str
    path: str
    commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    blob: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


class Provenance(m.Strict):
    imported_from: str
    outline_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    upstream: Upstream
    note: str
    integrations_from: str | None = None     # reviewed overlay merged by tools/import_ledger.py


class Ledger(m.Strict):
    ledger_version: Literal[1]
    provenance: Provenance
    entries: list[LedgerEntry]

    def by_id(self) -> dict[str, LedgerEntry]:
        return {e.id: e for e in self.entries}


def load_ledger(path: str | Path) -> Ledger:
    led = Ledger.model_validate_json(Path(path).read_text("utf-8"))
    ids = [e.id for e in led.entries]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate ledger ids")
    return led


def unavailable_reasons(e: LedgerEntry, detected_features: frozenset[str] = frozenset()) -> list[str]:
    reasons = []
    if e.status != "integrated" or e.adapter is None:
        reasons.append("no adapter integrated in this project yet")
    if e.status == "integrated" and not e.test_result:
        reasons.append("adapter has no recorded test result")
    if e.required_feature and e.required_feature not in detected_features:
        reasons.append(f"required dependency '{e.required_feature}' not detected on this host")
    return reasons


def availability(led: Ledger, detected_features: frozenset[str] = frozenset()) -> dict[str, list[str]]:
    """mode id -> reasons it is unavailable (empty list = available)."""
    return {e.id: unavailable_reasons(e, detected_features) for e in led.entries}


def summary(led: Ledger, detected_features: frozenset[str] = frozenset()) -> m.LedgerSummary:
    avail = availability(led, detected_features)
    return m.LedgerSummary(version=led.ledger_version, available=[i for i, r in avail.items() if not r])
