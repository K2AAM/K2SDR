-- Server-side recordings (M1 design §3.6). Files live under the recordings directory, and this table
-- is the catalog. Frequencies are integer Hz (B5: identical in state, filename and metadata).
CREATE TABLE recordings (
    id            TEXT PRIMARY KEY,           -- rec_...
    owner         TEXT NOT NULL,              -- principal, e.g. user:adam
    receiver      TEXT NOT NULL,
    frequency_hz  INTEGER NOT NULL,
    mode          TEXT NOT NULL,
    started_utc   TEXT NOT NULL,
    ended_utc     TEXT,
    status        TEXT NOT NULL,              -- recording | done | interrupted | storage_full | failed
    detail        TEXT,
    file          TEXT NOT NULL UNIQUE,       -- <UTC>_<frequency_hz>_<mode>.wav
    bytes         INTEGER NOT NULL DEFAULT 0,
    samples       INTEGER NOT NULL DEFAULT 0, -- 12 kS/s mono int16
    gaps          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX recordings_owner ON recordings(owner);
