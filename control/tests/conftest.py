from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_DIR = REPO_ROOT / "protocol"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def protocol_dir() -> Path:
    return PROTOCOL_DIR
