"""Board-bound claims and bearer sessions for the opt-in external agent pilot."""

from datetime import datetime, timedelta
from hashlib import sha256
import re
from secrets import token_urlsafe
from uuid import NAMESPACE_URL, uuid4, uuid5

CLAIM_PREFIX = "tbea_claim_"
BEARER_PREFIX = "tbea_"
SUFFIX = re.compile(r"[A-Za-z0-9_-]{43}")
CLAIM_TTL = timedelta(minutes=5)
SESSION_TTL = timedelta(hours=1)
ACTION_FIELDS = {
    "context": frozenset(),
    "my-next": frozenset({"project"}),
    "my-queue": frozenset({"project", "state", "before", "limit"}),
    "task": frozenset({"id"}),
    "advance-task": frozenset({"id", "transition", "note", "details", "force_text"}),
}
SCHEMA = """
    CREATE TABLE IF NOT EXISTS external_agent_enrollments (
        id TEXT PRIMARY KEY,
        principal TEXT NOT NULL UNIQUE REFERENCES board_principals (ledger_key),
        project TEXT NOT NULL REFERENCES projects (prefix),
        actions TEXT NOT NULL,
        claim_digest TEXT UNIQUE,
        claim_expires_at TEXT,
        bearer_digest TEXT UNIQUE,
        bearer_expires_at TEXT
    )
"""
TICKET_FIELDS = ("id", "key", "project", "title", "state", "dri", "note", "next_step", "event_id")


def valid_secret(value, prefix):
    return isinstance(value, str) and value.startswith(prefix) and SUFFIX.fullmatch(value[len(prefix):]) is not None


def digest(board, secret):
    return sha256(f"{board}\n{secret}".encode()).hexdigest()


def issue_claim(db, board, identifier, now):
    claim = CLAIM_PREFIX + token_urlsafe(32)
    expires = (now + CLAIM_TTL).isoformat()
    if not db.execute(
        "UPDATE external_agent_enrollments SET claim_digest = ?, claim_expires_at = ? WHERE id = ?",
        (digest(board, claim), expires, identifier),
    ).rowcount:
        return None
    return {"id": identifier, "claim": claim, "expires_at": expires}


def enroll(db, board, project, name, actions, now):
    identifier = str(uuid4())
    principal = f"external-agent:{identifier}"
    db.execute(
        """INSERT INTO board_principals (server_id, kind, subject, ledger_key, name)
           VALUES (?, 'agent', ?, ?, ?)""",
        (str(uuid5(NAMESPACE_URL, board + "external-agents")), identifier, principal, name),
    )
    db.execute(
        "INSERT INTO external_agent_enrollments (id, principal, project, actions) VALUES (?, ?, ?, ?)",
        (identifier, principal, project, ",".join(sorted(actions))),
    )
    return {"principal": principal, "project": project, "actions": sorted(actions),
            **issue_claim(db, board, identifier, now)}


def exchange(db, board, claim, now):
    if not valid_secret(claim, CLAIM_PREFIX):
        return None
    bearer = BEARER_PREFIX + token_urlsafe(32)
    expires = (now + SESSION_TTL).isoformat()
    row = db.execute(
        """UPDATE external_agent_enrollments
           SET claim_digest = NULL, claim_expires_at = NULL, bearer_digest = ?, bearer_expires_at = ?
           WHERE claim_digest = ? AND claim_expires_at > ?
           RETURNING principal, project""",
        (digest(board, bearer), expires, digest(board, claim), now.isoformat()),
    ).fetchone()
    if row is None:
        return None
    return {"token": bearer, "expires_at": expires, **dict(row)}


def authorize(db, board, bearer, now: datetime):
    if not valid_secret(bearer, BEARER_PREFIX):
        return None
    return db.execute(
        """SELECT principal, project, actions FROM external_agent_enrollments
           WHERE bearer_digest = ? AND bearer_expires_at > ?""",
        (digest(board, bearer), now.isoformat()),
    ).fetchone()


def enrollment_project(db, identifier):
    row = db.execute("SELECT project FROM external_agent_enrollments WHERE id = ?", (identifier,)).fetchone()
    return row["project"] if row else None


def enrolled_project(db, principal):
    row = db.execute("SELECT project FROM external_agent_enrollments WHERE principal = ?", (principal,)).fetchone()
    return row["project"] if row else None


def revoke(db, identifier):
    return db.execute(
        """UPDATE external_agent_enrollments SET claim_digest = NULL, claim_expires_at = NULL,
           bearer_digest = NULL, bearer_expires_at = NULL WHERE id = ?""",
        (identifier,),
    ).rowcount > 0


def ticket_summary(task):
    return {key: task[key] for key in TICKET_FIELDS if key in task}


def action_result(name, result):
    if name == "my-queue":
        return {**result, "items": [ticket_summary(task) for task in result["items"]]}
    if name == "task":
        return {"task": ticket_summary(result["task"]), "brief": result["brief"],
                "checklist": result["checklist"]}
    return result
