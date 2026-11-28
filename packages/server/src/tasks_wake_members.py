"""Durable, recipient-bound history of exact Inbox envelope members."""

from datetime import datetime, timezone
import json

import tasks_projects as projects
import tasks_ring1 as ring1
import tasks_storage

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS inbox_wake_members (
        id TEXT PRIMARY KEY,
        recipient TEXT NOT NULL REFERENCES board_principals (ledger_key),
        project TEXT NOT NULL REFERENCES projects (prefix),
        task_id BIGINT NOT NULL REFERENCES tasks (id),
        ticket TEXT NOT NULL,
        cue_kind TEXT NOT NULL,
        cue_key TEXT NOT NULL,
        source_event_id BIGINT,
        receipt_ref TEXT NOT NULL,
        acceptance TEXT NOT NULL CHECK (acceptance IN ('accepted', 'unknown')),
        observed_at TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS inbox_wake_members_recipient_history
       ON inbox_wake_members (recipient, project, observed_at, id)""",
    """CREATE TABLE IF NOT EXISTS inbox_wake_stalled_dispositions (
        recipient TEXT NOT NULL REFERENCES board_principals (ledger_key),
        task_id BIGINT NOT NULL REFERENCES tasks (id),
        source_event_id BIGINT NOT NULL REFERENCES events (id),
        acknowledged_at TEXT NOT NULL,
        PRIMARY KEY (recipient, task_id, source_event_id)
    )""",
)


def record(db, *, recipient, envelope, members, acceptance, at):
    at = datetime.fromisoformat(at).astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    changed = False
    for position, member in enumerate(members):
        project = projects.identifier(member.ticket)[0]
        result = db.execute(
            """INSERT INTO inbox_wake_members (
                id, recipient, project, task_id, ticket, cue_kind, cue_key, source_event_id, receipt_ref, acceptance,
                observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO NOTHING""",
            (f"{envelope}:{position:06d}", recipient, project, member.task_id, member.ticket, member.kind,
             member.key, member.source_event, envelope, acceptance, at),
        )
        changed |= bool(result.rowcount)
    return changed


def import_confirmed(db, state, scope, envelope):
    """Import one stopped sender journal only when an exact board acceptance exists."""
    from taskboard_server.inbox_batch import BatchMember
    import tasks_core as ledger

    journal = state.execute(
        "SELECT members, acceptance FROM inbox_batch_history WHERE event_key = ?", (envelope,),
    ).fetchone()
    saved = state.execute("SELECT * FROM sender WHERE singleton = 1").fetchone()
    if journal is None or journal["acceptance"] != "accepted" or saved is None:
        raise ValueError("Choose an existing accepted batch in the original sender journal.")
    with tasks_storage.write(db):
        current = scope.binding(db)
        fields = ("ledger", "board", "server", "recipient", "owner", "app")
        if current != tuple(saved[field] for field in fields):
            raise ValueError("The original sender journal has a different board or recipient binding.")
        owner = saved["owner"]
        receipt = db.execute(
            """SELECT at FROM audit_events WHERE actor = ? AND record_type = ?
               AND operation = 'accept-app-wake' AND record_id = ?""",
            (owner, f"app-wake:{scope.project}", envelope),
        ).fetchone()
        if receipt is None:
            raise ValueError("This sender journal has no exact board acceptance.")
        members = [BatchMember(**member) for member in json.loads(journal["members"])]
        for member in members:
            if projects.identifier(member.ticket)[0] != scope.project or not ring1.visible(db, scope.project, owner):
                raise ValueError("A journal member is outside the selected recipient and project.")
        changed = record(db, recipient=owner, envelope=envelope, members=members, acceptance="accepted", at=receipt["at"])
        if changed:
            ledger.bump_revision(db, scope.project)
        return len(members), changed


def acknowledge_stalled(db, recipient, task_id, source_event, at):
    result = db.execute(
        """INSERT INTO inbox_wake_stalled_dispositions VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING""",
        (recipient, task_id, source_event, at),
    )
    return bool(result.rowcount)


def listing(db, project, recipient, *, before, limit, principal, cue_kinds=()):
    source_visible, source_args = ring1.visibility(db, "w.project", principal)
    current_visible, current_args = ring1.visibility(db, "tasks.project_prefix", principal)
    filters = [source_visible, current_visible]
    args = [*source_args, *current_args]
    if project is not None:
        filters.append("w.project = ?")
        args.append(project)
    if recipient is not None:
        filters.append("w.recipient = ?")
        args.append(recipient)
    if cue_kinds:
        filters.append(f"w.cue_kind IN ({', '.join('?' for _ in cue_kinds)})")
        args.extend(cue_kinds)
    if before is not None:
        try:
            cursor = json.loads(before)
            if not isinstance(cursor, list) or len(cursor) != 2 or not all(isinstance(part, str) for part in cursor):
                raise ValueError
        except ValueError:
            raise ValueError("Use the cursor returned by wake-member history.") from None
        filters.append("(w.observed_at, w.id) < (?, ?)")
        args.extend(cursor)
    rows = db.execute(
        f"""SELECT w.*, p.name, p.subject,
                   COALESCE(stalled.acknowledged_at, legacy.acknowledged_at, goal.at) AS disposition_at,
                   goal.id AS disposition_event_id
            FROM inbox_wake_members AS w
            JOIN tasks ON tasks.id = w.task_id
            JOIN board_principals AS p ON p.ledger_key = w.recipient
            LEFT JOIN inbox_wake_stalled_dispositions AS stalled
                ON w.cue_kind = 'in_progress' AND stalled.recipient = w.recipient
                AND stalled.task_id = w.task_id AND stalled.source_event_id = w.source_event_id
            LEFT JOIN stalled_cue_acknowledgments AS legacy
                ON w.cue_kind = 'in_progress' AND legacy.owner = w.recipient
                AND legacy.task_id = w.task_id AND legacy.source_event_id = w.source_event_id
            LEFT JOIN events AS goal ON goal.id = (
                SELECT id FROM events WHERE w.cue_kind LIKE 'goal_%' AND task_id = w.task_id
                AND actor = w.recipient AND action = 'log' AND json_extract(context, '$.goal_occurrence') = w.cue_key
                ORDER BY id LIMIT 1
            )
            WHERE {' AND '.join(filters)}
            ORDER BY w.observed_at DESC, w.id DESC LIMIT ?""",
        (*args, limit + 1),
    ).fetchall()
    items = [{
        "id": row["id"], "project": row["project"], "ticket": row["ticket"],
        "recipient": {"id": row["subject"], "name": row["name"]},
        "carrier": "raft_app", "acceptance": row["acceptance"], "observed_at": row["observed_at"],
        "accepted_at": row["observed_at"] if row["acceptance"] == "accepted" else None,
        "receipt_ref": row["receipt_ref"], "cue_kind": row["cue_kind"], "cue_key": row["cue_key"],
        "source_event_id": row["source_event_id"], "source_ref": row["ticket"],
        "disposition_at": row["disposition_at"], "disposition_event_id": row["disposition_event_id"],
        "sort_key": [row["observed_at"], row["id"]],
    } for row in rows[:limit]]
    return {"items": items, "next_cursor": json.dumps(items[-1]["sort_key"]) if len(rows) > limit else None}
