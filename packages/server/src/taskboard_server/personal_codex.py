"""Ticket-scoped progress reporting for the personal Codex pilot."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
from secrets import token_urlsafe
from uuid import UUID, uuid4

import tasks_core as ledger
import tasks_peer_asks as peer_asks
import tasks_projects as projects


SESSION_DURATION = timedelta(hours=8)
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
REPLAY_KEY = re.compile(r"[A-Za-z0-9._:-]{1,128}")
CONNECT_PAGE = """
<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Personal Codex connections · Taskboard</title>
<style>
  :root { color-scheme: light dark; font-family: system-ui, sans-serif; }
  body { max-width: 40rem; margin: 2rem auto; padding: 0 1.25rem; line-height: 1.5; }
  input, button { font: inherit; padding: .375rem .5rem; }
  input { box-sizing: border-box; max-width: 100%; }
  form, li { margin-block: .75rem; }
  li form { display: inline; }
  #connection-token { display: block; font-family: monospace; width: min(100%, 26rem); }
</style>
<main>
  <h1>Personal Codex connections</h1>
  <p>Connections can read {{ ticket }} and add progress to its Activity.</p>
  {% if token %}
    <section aria-label="New connection">
      <label for="connection-token">Copy this token now. It is shown once.</label>
      <input id="connection-token" value="{{ token }}" readonly autocomplete="off" spellcheck="false">
      <p>Set <code>TASKBOARD_PERSONAL_CODEX_TOKEN</code> in the session environment.</p>
    </section>
  {% endif %}
  <form method="post" action="{{ connect_path }}">
    <label for="session">Session name</label>
    <input id="session" name="session" required maxlength="80" autocomplete="off">
    <button type="submit">Create connection</button>
  </form>
  <h2>Active connections</h2>
  {% if sessions %}
    <ul>
    {% for session in sessions %}
      <li>{{ session.label }} · expires {{ session.expires_at[:16] | replace('T', ' ') }} UTC
        <form method="post" action="{{ connect_path }}/revoke">
          <input type="hidden" name="connection" value="{{ session.id }}">
          <button type="submit" aria-label="Revoke {{ session.label }}">Revoke</button>
        </form>
      </li>
    {% endfor %}
    </ul>
  {% else %}
    <p>There are no active connections.</p>
  {% endif %}
</main>
</html>
"""
SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS personal_codex_connections (
        id TEXT PRIMARY KEY,
        token_hash TEXT NOT NULL UNIQUE,
        owner TEXT NOT NULL REFERENCES board_principals (ledger_key),
        task_id INTEGER NOT NULL REFERENCES tasks (id),
        label TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revoked_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS personal_codex_receipts (
        connection_id TEXT NOT NULL REFERENCES personal_codex_connections (id),
        replay_key TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        event_id INTEGER NOT NULL REFERENCES events (id),
        PRIMARY KEY (connection_id, replay_key)
    )
    """,
)


@dataclass(frozen=True)
class Pilot:
    ticket: str
    subject: str


def configuration(env):
    ticket = env.get("TASKS_PERSONAL_CODEX_PILOT_TICKET")
    subject = env.get("TASKS_PERSONAL_CODEX_PILOT_SUBJECT")
    if ticket is None and subject is None:
        return None
    try:
        project, number = projects.identifier(ticket)
        if ticket != f"{project}-{number}":
            raise ValueError
        return Pilot(ticket, str(UUID(subject)))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("Set a canonical personal Codex pilot ticket and a verified human subject UUID.") from error


def utc_now():
    return datetime.now(timezone.utc)


def list_connections(db, owner, number):
    return db.execute(
        """
        SELECT id, label, expires_at FROM personal_codex_connections
        WHERE owner = ? AND task_id = ? AND revoked_at IS NULL AND expires_at > ?
        ORDER BY expires_at DESC
        """, (owner, number, utc_now().isoformat()),
    ).fetchall()


def issue(db, number, owner, label):
    task = ledger.get_task(db, number)
    if ledger.task_goal_kind(db, number) or peer_asks.find(db, number):
        raise ValueError("Use an ordinary ticket for the pilot.")
    if task["dri"] != owner:
        if task["dri"] or task["display_state"] != "ready":
            raise ValueError("The pilot ticket must belong to you or be Ready and unassigned.")
        db.execute("UPDATE tasks SET dri = ? WHERE id = ?", (owner, number))
        ledger.record(
            db, number, "log", owner, "ready", "The human owner connected the personal Codex pilot.", None, [],
            {"ownership_action": "reassign", "previous_dri": "", "next_dri": owner},
        )
        ledger.bump_revision(db)
    token = token_urlsafe(32)
    expiry = (utc_now() + SESSION_DURATION).isoformat()
    db.execute(
        """
        INSERT INTO personal_codex_connections (id, token_hash, owner, task_id, label, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (str(uuid4()), sha256(token.encode()).hexdigest(), owner, number, label, expiry),
    )
    return token


def revoke(db, owner, number, connection):
    return db.execute(
        """
        UPDATE personal_codex_connections SET revoked_at = ?
        WHERE id = ? AND owner = ? AND task_id = ? AND revoked_at IS NULL
        """, (utc_now().isoformat(), connection, owner, number),
    ).rowcount


def authorized_connection(db, pilot, ticket, authorization):
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not TOKEN.fullmatch(token):
        raise PermissionError("A valid session connection is required.")
    connection = db.execute(
        """
        SELECT connections.id, connections.label, connections.owner, connections.task_id
        FROM personal_codex_connections AS connections
        JOIN board_principals AS owner ON owner.ledger_key = connections.owner
        WHERE connections.token_hash = ? AND connections.revoked_at IS NULL AND connections.expires_at > ?
          AND owner.kind = 'human' AND owner.subject = ?
        """, (sha256(token.encode()).hexdigest(), utc_now().isoformat(), pilot.subject),
    ).fetchone()
    if connection is None:
        raise PermissionError("A valid session connection is required.")
    task = ledger.find_task(db, connection["task_id"])
    if task is None or projects.row_reference(task) != ticket or task["dri"] != connection["owner"]:
        raise PermissionError("This ticket is unavailable to the connection.")
    return connection, task


def append_progress(db, connection, task, replay_key, headline, details):
    digest = sha256(json.dumps((headline, details), ensure_ascii=True).encode()).hexdigest()
    receipt = db.execute(
        """
        SELECT content_hash, event_id FROM personal_codex_receipts
        WHERE connection_id = ? AND replay_key = ?
        """, (connection["id"], replay_key),
    ).fetchone()
    if receipt is not None:
        if receipt["content_hash"] != digest:
            raise ValueError("This replay key already identifies a different checkpoint.")
        return receipt["event_id"]

    label, session = connection["label"], connection["id"]
    attribution = f"Reported by Codex session {label} ({session[:8]})."
    event = ledger.record(
        db, task["id"], "log", connection["owner"], task["state"], headline, None, [],
        {
            "activity_details": f"{attribution}\n\n{details}" if details else attribution,
            "personal_codex": {"connection_id": session, "label": label},
        },
    )
    db.execute(
        "INSERT INTO personal_codex_receipts VALUES (?, ?, ?, ?)",
        (session, replay_key, digest, event),
    )
    ledger.bump_revision(db)
    return event
