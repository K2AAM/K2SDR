"""Accounts, roles, sessions and the first-run setup token.

- Passwords: argon2id (argon2-cffi defaults). Plaintext is never stored or logged.
- Sessions: 256-bit random tokens; only SHA-256(token) is stored. Each session
  has its own CSRF token. Sessions expire, and can be revoked per user.
- First run: with no admin account, a single-use setup token is written to a
  file readable only by the service user. Its path is logged, never its value.
  The token is deleted as soon as the first admin exists.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "listener": frozenset({"listen"}),
    "operator": frozenset({"listen", "decode", "hw.retune", "hw.antenna", "scan", "record"}),
    "admin": frozenset({"listen", "decode", "hw.retune", "hw.antenna", "scan", "record", "admin"}),
}
MIN_PASSWORD = 12
MAX_PASSWORD = 1024
_USERNAME_OK = set("abcdefghijklmnopqrstuvwxyz0123456789._-")

_ph = PasswordHasher()
# Verifying against this keeps unknown-user logins as slow as wrong-password logins.
_DUMMY_HASH = _ph.hash(secrets.token_hex(16))


class AccountError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def check_username(name: str) -> str:
    n = name.strip().lower()
    if not 1 <= len(n) <= 64 or not set(n) <= _USERNAME_OK:
        raise AccountError("username: 1-64 characters from a-z 0-9 . _ -")
    return n


def check_password(pw: str) -> None:
    if not MIN_PASSWORD <= len(pw) <= MAX_PASSWORD:
        raise AccountError(f"password must be {MIN_PASSWORD}-{MAX_PASSWORD} characters")


def audit(conn: sqlite3.Connection, actor: str, action: str, detail: str | None = None) -> None:
    conn.execute("INSERT INTO audit(ts_utc, actor, action, detail) VALUES (?,?,?,?)",
                 (_iso(_now()), actor, action, detail))


def admin_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM users WHERE role='admin' AND disabled=0 LIMIT 1").fetchone() is not None


def create_user(conn: sqlite3.Connection, username: str, password: str, role: str, actor: str) -> int:
    username = check_username(username)
    check_password(password)
    if role not in ROLE_PERMISSIONS:
        raise AccountError(f"unknown role {role!r}")
    try:
        cur = conn.execute("INSERT INTO users(username, password_hash, role, created_utc) VALUES (?,?,?,?)",
                           (username, _ph.hash(password), role, _iso(_now())))
    except sqlite3.IntegrityError:
        raise AccountError("username already exists") from None
    audit(conn, actor, "user.create", f"{username} role={role}")
    return cur.lastrowid


def verify_login(conn: sqlite3.Connection, username: str, password: str) -> sqlite3.Row | None:
    """The user row if the credentials are right and the account is enabled, else None."""
    try:
        username = check_username(username)
    except AccountError:
        username = ""
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    try:
        _ph.verify(row["password_hash"] if row else _DUMMY_HASH, password[:MAX_PASSWORD])
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return None
    if row is None or row["disabled"]:
        return None
    return row


def set_disabled(conn: sqlite3.Connection, username: str, disabled: bool, actor: str) -> None:
    cur = conn.execute("UPDATE users SET disabled=? WHERE username=?", (int(disabled), check_username(username)))
    if cur.rowcount != 1:
        raise AccountError("no such user")
    audit(conn, actor, "user.disable" if disabled else "user.enable", username)


@dataclass(frozen=True)
class SessionInfo:
    token_hash: str
    user_id: int
    username: str
    role: str
    csrf_token: str

    @property
    def permissions(self) -> frozenset[str]:
        return ROLE_PERMISSIONS[self.role]


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn: sqlite3.Connection, user_id: int, hours: int, client_ip: str | None) -> tuple[str, str]:
    """Returns (cookie token, csrf token). The cookie token is not stored."""
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    now = _now()
    conn.execute("INSERT INTO sessions(token_hash, user_id, csrf_token, created_utc, expires_utc, client_ip) "
                 "VALUES (?,?,?,?,?,?)",
                 (_hash_token(token), user_id, csrf, _iso(now), _iso(now + timedelta(hours=hours)), client_ip))
    return token, csrf


def lookup_session(conn: sqlite3.Connection, token: str | None) -> SessionInfo | None:
    if not token or len(token) > 128:
        return None
    row = conn.execute(
        "SELECT s.token_hash, s.user_id, s.csrf_token, s.expires_utc, s.revoked, u.username, u.role, u.disabled "
        "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash=?", (_hash_token(token),)).fetchone()
    if row is None or row["revoked"] or row["disabled"]:
        return None
    if datetime.fromisoformat(row["expires_utc"]) <= _now():
        return None
    return SessionInfo(row["token_hash"], row["user_id"], row["username"], row["role"], row["csrf_token"])


def revoke_session(conn: sqlite3.Connection, token_hash: str) -> None:
    conn.execute("UPDATE sessions SET revoked=1 WHERE token_hash=?", (token_hash,))


def revoke_user_sessions(conn: sqlite3.Connection, username: str, actor: str) -> list[str]:
    """Revoke every session of a user; returns the revoked token hashes."""
    rows = conn.execute("SELECT s.token_hash FROM sessions s JOIN users u ON u.id=s.user_id "
                        "WHERE u.username=? AND s.revoked=0", (check_username(username),)).fetchall()
    conn.execute("UPDATE sessions SET revoked=1 WHERE user_id=(SELECT id FROM users WHERE username=?)",
                 (check_username(username),))
    audit(conn, actor, "sessions.revoke", f"{username} ({len(rows)})")
    return [r["token_hash"] for r in rows]


def csrf_ok(info: SessionInfo, presented: str | None) -> bool:
    return bool(presented) and hmac.compare_digest(info.csrf_token, presented)


# ------------------------------------------------------------ first-run setup

def ensure_setup_token(conn: sqlite3.Connection, path: Path) -> bool:
    """Create the single-use setup token if no admin exists. True if setup is pending."""
    if admin_exists(conn):
        if path.exists():
            path.unlink()
        return False
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(24) + "\n")
    return True


def setup_token_ok(path: Path, presented: str) -> bool:
    try:
        expected = path.read_text().strip()
    except FileNotFoundError:
        return False
    return bool(expected) and hmac.compare_digest(expected, presented.strip())


def complete_setup(conn: sqlite3.Connection, path: Path, presented: str, username: str, password: str) -> int:
    """Create the first admin with the setup token, then destroy the token."""
    if admin_exists(conn):
        raise AccountError("setup already completed")
    if not setup_token_ok(path, presented):
        raise PermissionError("bad setup token")
    uid = create_user(conn, username, password, "admin", actor="setup")
    path.unlink(missing_ok=True)
    audit(conn, f"user:{check_username(username)}", "setup.complete")
    return uid
