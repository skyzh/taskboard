"""Connections and publishing instructions for human-owned personal agents."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import re
from secrets import token_urlsafe
from uuid import uuid4

from tasks_storage import Postgres


API_TOKEN_PREFIX = "tbpa_"
API_TOKEN = re.compile(r"tbpa_[A-Za-z0-9_-]{43}")
CLI_CHALLENGE = re.compile(r"[0-9a-f]{64}")
CLI_VERIFIER = re.compile(r"[A-Za-z0-9_-]{43}")
MAX_DURATION = timedelta(hours=8)
LOGIN_WINDOW = timedelta(minutes=5)
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
CLI_SCHEMA = """
    CREATE TABLE IF NOT EXISTS personal_agent_cli_approvals (
        challenge TEXT PRIMARY KEY,
        owner TEXT NOT NULL REFERENCES board_principals (ledger_key),
        browser_session TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
"""


INSTRUCTIONS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS personal_agent_instructions (
        owner TEXT PRIMARY KEY REFERENCES board_principals (ledger_key),
        instructions TEXT NOT NULL
    )
"""
MAX_INSTRUCTIONS = 8000


def publishing_instructions(db, owner):
    row = db.execute(
        "SELECT instructions FROM personal_agent_instructions WHERE owner = ?", (owner,),
    ).fetchone()
    return row["instructions"] if row is not None else ""


def save_publishing_instructions(db, owner, instructions):
    if instructions == "" or isinstance(db, Postgres):
        db.execute("DELETE FROM personal_agent_instructions WHERE owner = ?", (owner,))
        if instructions:
            # PostgreSQL can log bound values. COPY keeps the text out of SQL
            # statements and parameter tracing. The caller holds the writer lock.
            with db.connection.cursor() as cursor, cursor.copy(
                "COPY personal_agent_instructions (owner, instructions) FROM STDIN"
            ) as copy:
                copy.write_row((owner, instructions))
    else:
        db.execute(
            """
            INSERT INTO personal_agent_instructions (owner, instructions) VALUES (?, ?)
            ON CONFLICT (owner) DO UPDATE SET instructions = excluded.instructions
            """, (owner, instructions),
        )


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


def authorize_cli(db, challenge, owner, browser_session):
    now = utc_now()
    db.execute("DELETE FROM personal_agent_cli_approvals WHERE expires_at <= ?", (now.isoformat(),))
    return db.execute(
        """
        INSERT INTO personal_agent_cli_approvals (challenge, owner, browser_session, expires_at)
        VALUES (?, ?, ?, ?) ON CONFLICT (challenge) DO NOTHING
        """, (challenge, owner, browser_session, (now + LOGIN_WINDOW).isoformat()),
    ).rowcount


def cli_approval(db, challenge):
    return db.execute(
        "SELECT owner, browser_session FROM personal_agent_cli_approvals WHERE challenge = ? AND expires_at > ?",
        (challenge, utc_now().isoformat()),
    ).fetchone()


def consume_cli_approval(db, challenge, owner, browser_session):
    return db.execute(
        """
        DELETE FROM personal_agent_cli_approvals
        WHERE challenge = ? AND owner = ? AND browser_session = ? AND expires_at > ?
        """, (challenge, owner, browser_session, utc_now().isoformat()),
    ).rowcount


def revoke_token(db, token):
    db.execute(
        "DELETE FROM personal_agent_connections WHERE token_hash = ?",
        (sha256(token.encode()).hexdigest(),),
    )
