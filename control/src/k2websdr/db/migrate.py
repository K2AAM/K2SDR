"""Apply numbered SQL migrations (db/migrations/NNNN_name.sql) in order.

Each migration runs in one transaction together with the schema_version
update, so a failed migration leaves the database at the previous version.
"""

from __future__ import annotations

import re
import sqlite3
from importlib import resources
from pathlib import Path

_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def migrations() -> list[tuple[int, str, str]]:
    out = []
    for f in resources.files("k2websdr.db").joinpath("migrations").iterdir():
        mt = _NAME.match(f.name)
        if mt:
            out.append((int(mt.group(1)), f.name, f.read_text("utf-8")))
    out.sort()
    nums = [n for n, _, _ in out]
    if nums != list(range(1, len(nums) + 1)):
        raise RuntimeError(f"migrations must be numbered 0001.. without gaps, found {nums}")
    return out


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT max(version) FROM schema_version").fetchone()
    return row[0] or 0


def migrate(conn: sqlite3.Connection) -> int:
    have = current_version(conn)
    for num, name, sql in migrations():
        if num <= have:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in (s.strip() for s in sql.split(";")):
                if stmt and not all(line.strip().startswith("--") or not line.strip() for line in stmt.splitlines()):
                    conn.execute(stmt)
            conn.execute("INSERT INTO schema_version(version) VALUES (?)", (num,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"migration {name} failed")
        have = num
    return have
