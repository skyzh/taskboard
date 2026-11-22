"""One editable recurring maintenance schedule per newly registered project."""

from taskboard_client.work import READ, WRITE

from datetime import timedelta
import json
from uuid import uuid4

import tasks_audit as audit
import tasks_projects as projects
import tasks_recurring as recurring
import watches


DEFAULT_HOURS = 7 * 24
DEFAULT_TITLE = "Review project maintenance"
DEFAULT_STEP = "Review the flagged tickets and use the project manager controls to correct ownership."
SCHEMA = """
    CREATE TABLE IF NOT EXISTS project_maintenance (
        project TEXT PRIMARY KEY REFERENCES projects (prefix),
        definition_id INTEGER NOT NULL UNIQUE REFERENCES recurring_definitions (id),
        members TEXT NOT NULL DEFAULT '[]'
    )
"""


def find(db, *, project=None, definition_id=None):
    if not watches.has_table(db, "project_maintenance"):
        return None
    field, value = ("project", project) if project is not None else ("definition_id", definition_id)
    return db.execute(
        f"""
        SELECT recurring_definitions.*, project_maintenance.project, project_maintenance.members,
               projects.dri AS manager
        FROM project_maintenance
        JOIN recurring_definitions ON recurring_definitions.id = project_maintenance.definition_id
        JOIN projects ON projects.prefix = project_maintenance.project
        WHERE project_maintenance.{field} = ?
        """, (value,),
    ).fetchone()


def describe(row, project):
    if row is None:
        return {"project": project, "schedule": None}
    return {
        "project": project,
        "schedule": {
            "id": f"R-{row['id']}", "owner": row["owner"], "state": row["state"],
            "cadence_hours": row["cadence_hours"], "next_at": row["next_at"],
            "members": json.loads(row["members"]),
        },
    }


def validate_owner(db, owner, live):
    if not owner or not owner.strip():
        raise ValueError("Choose a maintenance owner.")
    if live and (
        not watches.has_table(db, "board_principals")
        or not db.execute("SELECT 1 FROM board_principals WHERE ledger_key = ?", (owner,)).fetchone()
    ):
        raise ValueError("Choose a maintenance owner who has already signed in to this board.")


def members_json(values):
    if any(not value.strip() for value in values):
        raise ValueError("Provide nonempty maintenance roster entries.")
    return json.dumps(sorted(set(values)))


def next_slot(as_of, hours, requested):
    if hours <= 0:
        raise ValueError("Cadence must be a positive whole number of hours.")
    now = recurring.utc(as_of)
    recurring.checked_slot(now, hours)
    slot = recurring.utc(requested) if requested is not None else now + timedelta(hours=hours)
    if slot < now:
        raise ValueError("Choose a next check at or after the current UTC time.")
    recurring.checked_slot(slot, hours)
    return recurring.utc_text(slot)


def seed(db, project, manager, args, as_of, *, live):
    owner = args.maintenance_owner if args.maintenance_owner is not None else manager
    hours = args.maintenance_hours if args.maintenance_hours is not None else DEFAULT_HOURS
    validate_owner(db, owner, live)
    slot = next_slot(as_of, hours, args.maintenance_next_at)
    number, _ = recurring.create_definition(
        db, owner, f"project-maintenance-{uuid4().hex}", DEFAULT_TITLE, DEFAULT_STEP, hours, slot, as_of,
    )
    db.execute(
        "INSERT INTO project_maintenance (project, definition_id, members) VALUES (?, ?, ?)",
        (project, number, members_json(args.maintenance_members or [])),
    )
    if args.maintenance_state == "paused":
        db.execute("UPDATE recurring_definitions SET state = 'paused' WHERE id = ?", (number,))


def managed_schedule(db, project, actor):
    project = projects.require(db, project)
    if not projects.manages(db, project, actor):
        raise PermissionError("Only this project's manager can manage its maintenance schedule.")
    return project, find(db, project=project)


def update(db, args, as_of, *, live):
    project, row = managed_schedule(db, args.project, args.by)
    if row is None:
        raise ValueError(f"Project {project} has no maintenance schedule.")
    owner = row["owner"] if args.maintenance_owner is None else args.maintenance_owner
    if owner != row["owner"]:
        validate_owner(db, owner, live)
    hours = row["cadence_hours"] if args.maintenance_hours is None else args.maintenance_hours
    state = args.maintenance_state or row["state"]
    members = row["members"]
    if args.maintenance_members is not None:
        if args.clear_members:
            raise ValueError("Set or clear the roster in one update.")
        members = members_json(args.maintenance_members)
    elif args.clear_members:
        members = "[]"
    slot = row["next_at"]
    reschedule = args.maintenance_next_at is not None or hours != row["cadence_hours"] or (
        state == "active" and row["state"] == "paused"
    )
    if reschedule:
        slot = next_slot(as_of, hours, args.maintenance_next_at)
    if row["state"] == "active" and (reschedule or state == "paused"):
        recurring.record_due(db, row, recurring.utc(as_of))
    if reschedule:
        latest = db.execute(
            "SELECT MAX(due_at) FROM recurring_occurrences WHERE definition_id = ?", (row["id"],),
        ).fetchone()[0]
        if latest is not None and slot <= latest:
            raise ValueError("Schedule the next check after the last recorded occurrence.")
    else:
        slot = db.execute(
            "SELECT next_at FROM recurring_definitions WHERE id = ?", (row["id"],),
        ).fetchone()[0]
    if (owner, hours, state, slot, members) != (
        row["owner"], row["cadence_hours"], row["state"], row["next_at"], row["members"],
    ):
        db.execute(
            "UPDATE recurring_definitions SET owner = ?, cadence_hours = ?, state = ?, next_at = ? WHERE id = ?",
            (owner, hours, state, slot, row["id"]),
        )
        if members != row["members"]:
            db.execute("UPDATE project_maintenance SET members = ? WHERE project = ?", (members, project))
        audit.record(db, scope="private", actor=args.by, operation=WRITE,
                     record_type="project", record_id=project, at=as_of)
    return describe(find(db, project=project), project)
