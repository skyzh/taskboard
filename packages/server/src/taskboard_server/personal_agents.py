"""Revocable HTTP connections for human-owned personal agent sessions."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import re
from secrets import token_urlsafe
from uuid import uuid4


API_TOKEN_PREFIX = "tbpa_"
API_TOKEN = re.compile(r"tbpa_[A-Za-z0-9_-]{43}")
MAX_DURATION = timedelta(hours=8)
SCHEMA = """
    CREATE TABLE IF NOT EXISTS personal_agent_connections (
        id TEXT PRIMARY KEY,
        token_hash TEXT NOT NULL UNIQUE,
        owner TEXT NOT NULL REFERENCES board_principals (ledger_key),
        browser_session TEXT NOT NULL,
        label TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
"""


def utc_now():
    return datetime.now(timezone.utc)


def list_connections(db, owner):
    return db.execute(
        """
        SELECT id, label, expires_at FROM personal_agent_connections
        WHERE owner = ? AND expires_at > ? ORDER BY expires_at DESC, id
        """, (owner, utc_now().isoformat()),
    ).fetchall()


def issue(db, owner, browser_session, remaining_seconds, label):
    token = API_TOKEN_PREFIX + token_urlsafe(32)
    expiry = (utc_now() + min(MAX_DURATION, timedelta(seconds=remaining_seconds))).isoformat()
    db.execute(
        "DELETE FROM personal_agent_connections WHERE owner = ? AND expires_at <= ?",
        (owner, utc_now().isoformat()),
    )
    db.execute(
        """
        INSERT INTO personal_agent_connections (id, token_hash, owner, browser_session, label, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (str(uuid4()), sha256(token.encode()).hexdigest(), owner, browser_session, label, expiry),
    )
    return {"token": token, "expires_at": expiry}


def revoke(db, owner, identifier):
    return db.execute(
        "DELETE FROM personal_agent_connections WHERE id = ? AND owner = ?",
        (identifier, owner),
    ).rowcount


def authorized_connection(db, token):
    if not API_TOKEN.fullmatch(token):
        return None
    return db.execute(
        """
        SELECT owner, browser_session FROM personal_agent_connections
        WHERE token_hash = ? AND expires_at > ?
        """, (sha256(token.encode()).hexdigest(), utc_now().isoformat()),
    ).fetchone()
