"""Server-side recordings (M1 design §3.6; outline §10 "reliable WAV audio download first").

- Clean demodulated audio: the channel's 12 kS/s mono int16, exactly as the worker made it
  (upstream of everything a listener does in the browser).
- Filenames `<UTC>_<frequency_hz>_<mode>.wav`, e.g. `20261027T183000Z_7074000_usb.wav`; the same
  integer Hz is in the catalog and the state (B5).
- The RIFF header is rewritten every few seconds, so a file is playable up to that point even if the
  service dies mid-recording.
- Storage is bounded by an admin-set cap and a free-space floor. Reaching either refuses new
  recordings and ends active ones (status storage_full); nothing is deleted automatically.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

RATE_HZ = 12000
HEADER_REFRESH_S = 5.0
_SAFE_MODE = re.compile(r"^[a-z0-9-]{1,32}$")


def wav_header(data_bytes: int, rate: int = RATE_HZ) -> bytes:
    """44-byte PCM WAV header, mono int16."""
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + data_bytes, b"WAVE", b"fmt ", 16, 1, 1, rate, rate * 2, 2,
                       16, b"data", data_bytes)


def file_name(started: datetime, frequency_hz: int, mode: str) -> str:
    if not _SAFE_MODE.match(mode) or not isinstance(frequency_hz, int) or frequency_hz < 0:
        raise ValueError("bad mode or frequency")
    return f"{started.strftime('%Y%m%dT%H%M%SZ')}_{frequency_hz}_{mode}.wav"


class WavWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._f = open(path, "xb")                     # never overwrite an existing file
        self._f.write(wav_header(0))
        self.data_bytes = 0
        self._refreshed = time.monotonic()

    def write(self, pcm: bytes) -> None:
        self._f.write(pcm)
        self.data_bytes += len(pcm)
        if time.monotonic() - self._refreshed >= HEADER_REFRESH_S:
            self._rewrite_header()

    def _rewrite_header(self) -> None:
        self._f.flush()
        pos = self._f.tell()
        self._f.seek(0)
        self._f.write(wav_header(self.data_bytes))
        self._f.seek(pos)
        self._f.flush()
        self._refreshed = time.monotonic()

    def close(self) -> None:
        if not self._f.closed:
            self._rewrite_header()
            os.fsync(self._f.fileno())
            self._f.close()

    @property
    def file_bytes(self) -> int:
        return 44 + self.data_bytes


@dataclass(frozen=True)
class StoragePolicy:
    directory: Path
    cap_bytes: int = 2 * 1024 ** 3                     # admin-set (config recordings_cap_bytes)
    min_free_bytes: int = 512 * 1024 ** 2              # never fill the disk below this


class RecordingCatalog:
    """The recordings table plus the directory it describes."""

    def __init__(self, conn: sqlite3.Connection, policy: StoragePolicy) -> None:
        self.conn = conn
        self.policy = policy
        policy.directory.mkdir(parents=True, exist_ok=True)

    def used_bytes(self) -> int:
        row = self.conn.execute("SELECT coalesce(sum(bytes), 0) FROM recordings").fetchone()
        return int(row[0])

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.policy.directory).free

    def full_reason(self, extra_bytes: int = 0) -> str | None:
        """Why no more audio may be written, or None."""
        if self.used_bytes() + extra_bytes >= self.policy.cap_bytes:
            return f"recording storage cap reached ({self.policy.cap_bytes} bytes)"
        if self.free_bytes() - extra_bytes <= self.policy.min_free_bytes:
            return "disk nearly full"
        return None

    def unique_path(self, name: str) -> Path:
        p = self.policy.directory / name
        n = 1
        while p.exists():
            p = self.policy.directory / name.replace(".wav", f"-{n}.wav")
            n += 1
        return p

    def insert(self, rec_id: str, owner: str, receiver: str, frequency_hz: int, mode: str, started_utc: str,
               file: str) -> None:
        self.conn.execute("INSERT INTO recordings(id, owner, receiver, frequency_hz, mode, started_utc, status, file) "
                          "VALUES (?, ?, ?, ?, ?, ?, 'recording', ?)",
                          (rec_id, owner, receiver, frequency_hz, mode, started_utc, file))

    def update(self, rec_id: str, *, bytes_: int, samples: int, gaps: int, status: str | None = None,
               detail: str | None = None, ended: bool = False) -> None:
        ended_utc = datetime.now(timezone.utc).isoformat() if ended else None
        self.conn.execute("UPDATE recordings SET bytes=?, samples=?, gaps=?, status=coalesce(?, status), "
                          "detail=coalesce(?, detail), ended_utc=coalesce(?, ended_utc) WHERE id=?",
                          (bytes_, samples, gaps, status, detail, ended_utc, rec_id))

    def mark_unfinished_interrupted(self) -> int:
        """At startup: recordings still 'recording' belong to a previous process."""
        cur = self.conn.execute("UPDATE recordings SET status='interrupted', detail='service restarted' "
                                "WHERE status='recording'")
        return cur.rowcount

    def list(self, owner: str | None) -> list[dict]:
        q = "SELECT * FROM recordings" + (" WHERE owner=?" if owner else "") + " ORDER BY started_utc DESC"
        return [dict(r) for r in self.conn.execute(q, (owner,) if owner else ())]

    def get(self, rec_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM recordings WHERE id=?", (rec_id,)).fetchone()
        return dict(r) if r else None

    def path_of(self, row: dict) -> Path:
        p = (self.policy.directory / row["file"]).resolve()
        if p.parent != self.policy.directory.resolve():
            raise ValueError("recording file outside the recordings directory")
        return p

    def delete(self, rec_id: str) -> None:
        row = self.get(rec_id)
        if row is None:
            return
        try:
            self.path_of(row).unlink()
        except FileNotFoundError:
            pass
        self.conn.execute("DELETE FROM recordings WHERE id=?", (rec_id,))
