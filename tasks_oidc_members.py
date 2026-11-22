"""Bind verified OpenID Connect subjects to existing Taskboard principals."""

from urllib.parse import urlsplit
from uuid import uuid4

import tasks_core as ledger
import tasks_profiles as profiles


SCHEMA = """
    CREATE TABLE IF NOT EXISTS oidc_members (
        issuer TEXT NOT NULL,
        subject TEXT NOT NULL,
        ledger_key TEXT NOT NULL UNIQUE REFERENCES board_principals (ledger_key),
        admitted INTEGER NOT NULL DEFAULT 1 CHECK (admitted IN (0, 1)),
        PRIMARY KEY (issuer, subject)
    )
"""


def issuer_url(value):
    if not isinstance(value, str):
        raise ValueError("Set an exact HTTPS OpenID Connect issuer URL.")
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError as error:
        raise ValueError("Set an exact HTTPS OpenID Connect issuer URL.") from error
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
        or "?" in value or "#" in value or any(character.isspace() for character in value)
    ):
        raise ValueError("Set an exact HTTPS OpenID Connect issuer URL.")
    return value


def valid_subject(value):
    return isinstance(value, str) and 0 < len(value) <= 255 and all(32 <= ord(char) < 127 for char in value)


def existing_issuer(db):
    row = db.execute("SELECT value FROM board_settings WHERE key = 'oidc_issuer'").fetchone()
    return row["value"] if row else None


def bind_issuer(db, issuer):
    previous = existing_issuer(db)
    if previous is not None and previous != issuer:
        raise ValueError("This board is bound to a different OpenID Connect issuer.")
    if previous is None:
        db.execute("INSERT INTO board_settings VALUES ('oidc_issuer', ?)", (issuer,))
    db.execute(SCHEMA)


def member(db, issuer, subject):
    row = db.execute(
        "SELECT ledger_key FROM oidc_members WHERE issuer = ? AND subject = ? AND admitted = 1",
        (issuer, subject),
    ).fetchone()
    return profiles.get(db, row["ledger_key"]) if row else None


def ensure_principal(db, ledger_key, name):
    if ledger_key is not None:
        current = profiles.get(db, ledger_key)
        if current is not None:
            if current["kind"] != "human":
                raise ValueError("Bind OpenID Connect to an existing human account.")
            return ledger_key
        if not db.execute(
            """
            SELECT 1 FROM tasks WHERE dri = ? UNION ALL
            SELECT 1 FROM events WHERE actor = ? OR waiting_on = ? LIMIT 1
            """, (ledger_key, ledger_key, ledger_key),
        ).fetchone():
            raise ValueError("This account key does not appear in the selected ledger.")
    account = str(uuid4())
    ledger_key = ledger_key or f"account:{account}"
    db.execute(
        "INSERT INTO board_principals (server_id, kind, subject, ledger_key, name) VALUES (?, 'human', ?, ?, ?)",
        (account, account, ledger_key, name or ledger_key),
    )
    return ledger_key


def set_membership(db, issuer, subject, *, admitted, ledger_key=None, name=None):
    current = db.execute(
        "SELECT ledger_key, admitted FROM oidc_members WHERE issuer = ? AND subject = ?", (issuer, subject),
    ).fetchone()
    if current is not None:
        if ledger_key is not None and current["ledger_key"] != ledger_key:
            raise ValueError("This OpenID Connect subject already has a different account binding.")
        changed = current["admitted"] != admitted
        if changed:
            db.execute("UPDATE oidc_members SET admitted = ? WHERE issuer = ? AND subject = ?",
                       (int(admitted), issuer, subject))
        return current["ledger_key"], changed
    if not admitted:
        raise ValueError("This OpenID Connect subject has no membership.")
    if ledger_key is not None and db.execute(
        "SELECT 1 FROM oidc_members WHERE ledger_key = ?", (ledger_key,),
    ).fetchone():
        raise ValueError("This account already has an OpenID Connect subject binding.")
    account = ensure_principal(db, ledger_key, name)
    db.execute("INSERT INTO oidc_members VALUES (?, ?, ?, 1)", (issuer, subject, account))
    return account, True


def admit(db, issuer, subject, name):
    actor = member(db, issuer, subject)
    if actor is None:
        return False
    if isinstance(name, str):
        name = name.strip()
        if 0 < len(name) <= 120 and name.isprintable() and name != actor["name"]:
            db.execute("UPDATE board_principals SET name = ? WHERE ledger_key = ?", (name, actor["ledger_key"]))
            ledger.bump_revision(db)
    return True
