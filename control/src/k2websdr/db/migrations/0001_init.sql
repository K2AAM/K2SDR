-- 0001: accounts, sessions, audit log.
-- Session tokens are never stored: only their SHA-256 hash.

CREATE TABLE users (
    id            INTEGER PRIMARY KEY,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE
                          CHECK (length(username) BETWEEN 1 AND 64),
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL CHECK (role IN ('listener', 'operator', 'admin')),
    disabled      INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
    created_utc   TEXT    NOT NULL
);

CREATE TABLE sessions (
    token_hash    TEXT    PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token    TEXT    NOT NULL,
    created_utc   TEXT    NOT NULL,
    expires_utc   TEXT    NOT NULL,
    revoked       INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
    client_ip     TEXT
);
CREATE INDEX sessions_user ON sessions(user_id);

CREATE TABLE audit (
    id     INTEGER PRIMARY KEY,
    ts_utc TEXT NOT NULL,
    actor  TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT
);
