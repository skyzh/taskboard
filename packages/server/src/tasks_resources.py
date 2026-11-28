"""Project-scoped resource definitions, fenced timed claims, and immutable history."""

from datetime import datetime, timedelta
import json
from uuid import uuid4

import tasks_core as ledger


SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS project_resources (
        project TEXT NOT NULL REFERENCES projects (prefix),
        resource_key TEXT NOT NULL,
        title TEXT NOT NULL,
        conditions TEXT NOT NULL,
        max_minutes INTEGER NOT NULL CHECK (max_minutes > 0),
        last_event INTEGER NOT NULL DEFAULT 0,
        holder TEXT,
        PRIMARY KEY (project, resource_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_resource_events (
        project TEXT NOT NULL,
        resource_key TEXT NOT NULL,
        event_id INTEGER NOT NULL,
        action TEXT NOT NULL CHECK (action IN ('claim', 'renew', 'release', 'expire')),
        at TEXT NOT NULL,
        actor TEXT,
        request_key TEXT,
        request_body TEXT,
        holder TEXT NOT NULL,
        PRIMARY KEY (project, resource_key, event_id),
        UNIQUE (project, resource_key, actor, request_key),
        FOREIGN KEY (project, resource_key) REFERENCES project_resources (project, resource_key)
    )
    """,
)


def resource(db, project, key):
    row = db.execute(
        "SELECT * FROM project_resources WHERE project = ? AND resource_key = ?", (project, key),
    ).fetchone()
    if row is None:
        raise LookupError("This project resource does not exist.")
    return dict(row)


def listing(db, project):
    return db.execute("SELECT * FROM project_resources WHERE project = ? ORDER BY resource_key", (project,))


def define(db, project, key, title, conditions, max_minutes):
    changed = db.execute("""
        INSERT INTO project_resources (project, resource_key, title, conditions, max_minutes)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (project, resource_key) DO UPDATE SET
            title = excluded.title, conditions = excluded.conditions, max_minutes = excluded.max_minutes
        WHERE project_resources.title <> excluded.title OR project_resources.conditions <> excluded.conditions
           OR project_resources.max_minutes <> excluded.max_minutes
    """, (project, key, title, conditions, max_minutes)).rowcount
    if changed:
        ledger.bump_revision(db, project)
    return resource(db, project, key)


def current_claim(row, now):
    if row["holder"] is not None:
        claim = json.loads(row["holder"])
        if claim["expires_at"] > now:
            return claim
    return None


def expiry(row, now):
    if row["holder"] is not None and current_claim(row, now) is None:
        claim = json.loads(row["holder"])
        return {"event_id": row["last_event"] + 1, "action": "expire", "at": claim["expires_at"],
                "actor": None, "holder": row["holder"]}
    return None


def history(db, row, now, *, before, limit):
    # Expose an elapsed lease immediately. The next acquisition persists this same event ID.
    elapsed = expiry(row, now)
    events = [elapsed] if elapsed and (before is None or elapsed["event_id"] < before) else []
    events.extend(db.execute("""
        SELECT event_id, action, at, actor, holder FROM project_resource_events
        WHERE project = ? AND resource_key = ? AND (? IS NULL OR event_id < ?)
        ORDER BY event_id DESC LIMIT ?
    """, (row["project"], row["resource_key"], before, before, limit + 1)))
    return events[:limit], events[limit - 1]["event_id"] if len(events) > limit else None


def append(db, row, action, at, holder, *, actor=None, request_key=None, request_body=None):
    number = row["last_event"] + 1
    encoded = json.dumps(holder)
    db.execute("""
        INSERT INTO project_resource_events
        (project, resource_key, event_id, action, at, actor, request_key, request_body, holder)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (row["project"], row["resource_key"], number, action, at, actor, request_key, request_body, encoded))
    row["last_event"] = number
    row["holder"] = encoded if action in ("claim", "renew") else None
    db.execute("""
        UPDATE project_resources SET last_event = ?, holder = ? WHERE project = ? AND resource_key = ?
    """, (number, row["holder"], row["project"], row["resource_key"]))
    return {"event_id": number, "action": action, "at": at, "actor": actor, "holder": encoded}


def change(db, row, actor, action, request_key, inputs, now):
    owner = actor["id"]
    body = json.dumps({"action": action, **inputs}, sort_keys=True)
    previous = db.execute("""
        SELECT event_id, action, at, actor, holder, request_body FROM project_resource_events
        WHERE project = ? AND resource_key = ? AND actor = ? AND request_key = ?
    """, (row["project"], row["resource_key"], owner, request_key)).fetchone()
    if previous is not None:
        if previous["request_body"] != body:
            raise ValueError("This request key was already used with different inputs.")
        return previous

    if action in ("claim", "renew") and inputs["minutes"] > row["max_minutes"]:
        raise ValueError("Choose a duration within this resource's maximum.")
    active = current_claim(row, now)
    if action == "claim":
        if active is not None:
            raise ValueError("This resource is already claimed.")
        if elapsed := expiry(row, now):
            append(db, row, "expire", elapsed["at"], json.loads(elapsed["holder"]))
        holder = {
            "claim_id": str(uuid4()), "holder": actor, "claimed_at": now, "revision": 1,
            "ticket": inputs["ticket"], "pr_url": inputs["pr_url"], "ticket_id": inputs["ticket_id"],
        }
    else:
        if active is None or active["claim_id"] != inputs["claim_id"] or active["revision"] != inputs["revision"]:
            raise ValueError("This claim expired or changed. Refresh the current holder before continuing.")
        if active["holder"]["id"] != owner:
            raise PermissionError("Only the current holder can change this claim.")
        holder = active
        if action == "renew":
            holder["revision"] += 1
    if action != "release":
        expires = datetime.fromisoformat(now) + timedelta(minutes=inputs["minutes"])
        holder["expires_at"] = expires.isoformat(timespec="seconds").replace("+00:00", "Z")
    result = append(db, row, action, now, holder, actor=owner, request_key=request_key, request_body=body)
    ledger.bump_revision(db, row["project"])
    return result
