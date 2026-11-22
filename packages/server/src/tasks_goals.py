"""A project assignee's dated and standing goals."""

import json
from types import SimpleNamespace

import tasks_core as ledger
import tasks_projects as projects
import tasks_storage


SCHEMA = """
    CREATE TABLE IF NOT EXISTS board_project_goals (
        project TEXT NOT NULL REFERENCES projects (prefix),
        owner TEXT NOT NULL REFERENCES board_principals (ledger_key),
        summary TEXT NOT NULL,
        deadline TEXT NOT NULL,
        ticket_ids TEXT NOT NULL,
        PRIMARY KEY (project, owner)
    )
"""

STANDING_SCHEMA = """
    CREATE TABLE IF NOT EXISTS board_standing_goals (
        project TEXT NOT NULL REFERENCES projects (prefix),
        owner TEXT NOT NULL REFERENCES board_principals (ledger_key),
        summary TEXT NOT NULL,
        PRIMARY KEY (project, owner)
    )
"""
STANDING_LIMIT = 500

MILESTONE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS board_goal_milestones (
        project TEXT NOT NULL REFERENCES projects (prefix),
        owner TEXT NOT NULL REFERENCES board_principals (ledger_key),
        summary TEXT NOT NULL,
        deadline TEXT NOT NULL,
        ticket_ids TEXT NOT NULL,
        PRIMARY KEY (project, owner, summary)
    )
"""

GOAL_TICKET_SCHEMA = """
    CREATE TABLE IF NOT EXISTS task_goals (
        task_id INTEGER PRIMARY KEY REFERENCES tasks (id),
        kind TEXT NOT NULL CHECK (kind IN ('standing', 'original', 'milestone')),
        deadline TEXT,
        ticket_ids TEXT NOT NULL,
        retired INTEGER NOT NULL DEFAULT 0
    )
"""
GOAL_MIGRATION = "goal_tickets_migrated"
LEGACY_GOAL_STEP = "Start or link a concrete investigation when there is work to do on this goal."
STANDING_EXPLORATION_LABEL = "Explore this goal"
STANDING_EXPLORATION_START = "Start this goal ticket and note what you plan to check."
PREVIOUS_STANDING_EXPLORATION = (
    f"{STANDING_EXPLORATION_START} Explore the product, recent changes, or workflow evidence. "
    "Open or link a focused ticket if you find a bug or improvement. Otherwise, record what you checked."
)
STANDING_EXPLORATION = (
    PREVIOUS_STANDING_EXPLORATION + " If you open a ticket from this standing goal, add the spontaneous tag."
)


def ensure_extended_schema(db):
    db.execute(STANDING_SCHEMA)
    db.execute(MILESTONE_SCHEMA)


def migrate_goal_tickets(db):
    """Carry legacy project goals into ordinary ticket records in the caller's transaction."""
    db.execute(GOAL_TICKET_SCHEMA)
    if db.execute("SELECT 1 FROM board_settings WHERE key = ?", (GOAL_MIGRATION,)).fetchone():
        return
    rows = db.execute("""
        SELECT project, owner, summary, 'standing' AS kind, NULL AS deadline, '[]' AS ticket_ids
        FROM board_standing_goals
        UNION ALL
        SELECT project, owner, summary, 'original', deadline, ticket_ids FROM board_project_goals
        UNION ALL
        SELECT project, owner, summary, 'milestone', deadline, ticket_ids FROM board_goal_milestones
        ORDER BY project, owner, kind, summary
    """).fetchall()
    for row in rows:
        create_goal_ticket(db, row["project"], row["owner"], row["kind"], row["summary"],
                           row["deadline"], row["ticket_ids"])
    if rows:
        db.execute("INSERT INTO board_settings (key, value) VALUES (?, '1')", (GOAL_MIGRATION,))
        ledger.bump_revision(db)


def restore_legacy_goals(db):
    """Project current goals for an older server before reverting the application."""
    with tasks_storage.write(db):
        if not tasks_storage.table_columns(db, "task_goals"):
            return 0
        owners = [row[0] for row in db.execute(
            "SELECT DISTINCT tasks.dri FROM tasks JOIN task_goals ON tasks.id = task_goals.task_id"
        )]
        rows = goal_rows(db, owners)
        migrated = db.execute("SELECT 1 FROM board_settings WHERE key = ?", (GOAL_MIGRATION,)).fetchone()
        if not rows and not migrated:
            return 0
        for table in ("board_standing_goals", "board_project_goals", "board_goal_milestones"):
            db.execute(f"DELETE FROM {table}")
        for row in rows:
            common = (row["project_prefix"], row["owner"], row["summary"])
            if row["kind"] == "standing":
                db.execute("INSERT INTO board_standing_goals VALUES (?, ?, ?)", common)
            else:
                table = "board_project_goals" if row["kind"] == "original" else "board_goal_milestones"
                db.execute(f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?)",
                           (*common, row["deadline"], row["ticket_ids"]))
            if row["state"] == "parked":
                args = SimpleNamespace(id=row["id"], by=row["owner"], note="Restored as a legacy goal.", related=None)
                ledger.close_task(db, args, [])
            db.execute("UPDATE task_goals SET retired=1 WHERE task_id=?", (row["id"],))
        db.execute("DELETE FROM board_settings WHERE key = ?", (GOAL_MIGRATION,))
        ledger.bump_revision(db)
        return len(rows)


def create_goal_ticket(db, project, owner, kind, summary, deadline, encoded_ids):
    args = SimpleNamespace(
        project=project, dri=owner, by=owner, title=summary, brief="",
        step=STANDING_EXPLORATION if kind == "standing" else LEGACY_GOAL_STEP,
    )
    task_id = ledger.create_task_record(db, args, [], state="parked")
    db.execute(
        "INSERT INTO task_goals (task_id, kind, deadline, ticket_ids) VALUES (?, ?, ?, ?)",
        (task_id, kind, deadline, encoded_ids),
    )
    return task_id


def goal_rows(db, owners, project=None, kind=None):
    if not owners:
        return []
    placeholders = ", ".join("?" for _ in owners)
    filters = [f"tasks.dri IN ({placeholders})", "events.state <> 'done'", "goals.retired = 0"]
    args = list(owners)
    if project is not None:
        filters.append("tasks.project_prefix = ?")
        args.append(project)
    if kind is not None:
        filters.append("goals.kind = ?")
        args.append(kind)
    return db.execute(f"""
        SELECT tasks.id, tasks.project_prefix, tasks.project_number, tasks.dri AS owner, tasks.title AS summary,
               goals.kind, goals.deadline, goals.ticket_ids, events.state
        FROM task_goals AS goals JOIN tasks ON tasks.id = goals.task_id
        JOIN events ON events.id = (
            SELECT MAX(id) FROM events WHERE task_id = tasks.id AND action NOT IN ('source-event', 'log')
        )
        WHERE {' AND '.join(filters)}
        ORDER BY tasks.project_prefix,
                 CASE goals.kind WHEN 'standing' THEN 0 WHEN 'original' THEN 1 ELSE 2 END,
                 goals.deadline, tasks.title
    """, args).fetchall()


def standing(db, owner, project=None, *, include_ticket=False):
    return [{"project": row["project_prefix"], "summary": row["summary"],
             **({"ticket": projects.row_reference(row)} if include_ticket else {})}
            for row in goal_rows(db, [owner], project, "standing")]


def write_goal(db, project, owner, kind, row, goal):
    if goal is None:
        if row is not None:
            args = SimpleNamespace(id=row["id"], by=owner, note="Removed this goal.", related=None)
            ledger.close_task(db, args, [])
        return
    summary, deadline, ids = goal
    encoded = json.dumps(ids)
    if row is None:
        create_goal_ticket(db, project, owner, kind, summary, deadline, encoded)
        return
    if (summary, deadline, encoded) == (row["summary"], row["deadline"], row["ticket_ids"]):
        return
    db.execute("UPDATE tasks SET title = ? WHERE id = ?", (summary, row["id"]))
    db.execute("UPDATE task_goals SET deadline = ?, ticket_ids = ? WHERE task_id = ?",
               (deadline, encoded, row["id"]))
    current = ledger.get_task(db, row["id"])
    ledger.record(db, row["id"], "log", owner, current["state"], "Updated this goal.", None, [])


def save_standing(db, project, owner, summary):
    row = next(iter(goal_rows(db, [owner], project, "standing")), None)
    write_goal(db, project, owner, "standing", row, (summary, None, []) if summary is not None else None)
    return {"project": project, "summary": summary} if summary is not None else None


def report_summaries(db, project, owners):
    summaries = {}
    for row in goal_rows(db, owners, project):
        summaries.setdefault(row["owner"], []).append({
            "kind": row["kind"], "summary": row["summary"], "deadline": row["deadline"],
            "ticket": projects.row_reference(row),
        })
    return summaries


def ticket_details(db, task):
    if not tasks_storage.table_columns(db, "task_goals"):
        return None
    row = db.execute("SELECT kind, deadline, ticket_ids FROM task_goals WHERE task_id=? AND retired=0",
                     (task["id"],)).fetchone()
    if row is None:
        return None
    return {"kind": row["kind"], "deadline": row["deadline"],
            "tickets": tickets(db, projects.row_project(task), task["dri"], json.loads(row["ticket_ids"]))}


def tickets(db, project, owner, ids):
    if not ids:
        return []
    placeholders = ", ".join("?" for _ in ids)
    rows = db.execute(
        f"""
        SELECT id, title, project_prefix, project_number
        FROM tasks
        WHERE project_prefix = ? AND dri = ? AND id IN ({placeholders})
        """,
        (project, owner, *ids),
    )
    by_id = {
        row["id"]: {"id": row["id"], "key": projects.row_reference(row), "title": row["title"]}
        for row in rows
    }
    return [by_id[number] for number in ids if number in by_id]


def get(db, project, owner):
    row = next(iter(goal_rows(db, [owner], project, "original")), None)
    return dated_goal(db, project, owner, row) if row is not None else None


def dated_goal(db, project, owner, row):
    return {
        "summary": row["summary"],
        "deadline": row["deadline"],
        "tickets": tickets(db, project, owner, json.loads(row["ticket_ids"])),
    }


def save(db, project, owner, goal):
    row = next(iter(goal_rows(db, [owner], project, "original")), None)
    related = owned_tickets(db, project, owner, goal[2]) if goal is not None else None
    write_goal(db, project, owner, "original", row, goal)
    return {"summary": goal[0], "deadline": goal[1], "tickets": related} if goal is not None else None


def owned_tickets(db, project, owner, ids):
    related = tickets(db, project, owner, ids)
    if len(related) != len(ids):
        raise ValueError("Choose tickets you own in this project.")
    return related


def milestones(db, project, owner):
    return [dated_goal(db, project, owner, row) for row in goal_rows(db, [owner], project, "milestone")]


def save_milestone(db, project, owner, previous, goal):
    rows = {row["summary"]: row for row in goal_rows(db, [owner], project, "milestone")}
    row = rows.get(previous)
    if goal is None:
        write_goal(db, project, owner, "milestone", row, None)
        return None
    summary, deadline, ids = goal
    related = owned_tickets(db, project, owner, ids)
    if summary != previous and summary in rows:
        if previous is None:
            raise ValueError("A milestone with this name already exists. Edit it or choose a different name.")
        raise ValueError("Choose a different milestone name.")
    if previous is not None and row is None:
        raise LookupError("Milestone not found in your goals.")
    write_goal(db, project, owner, "milestone", row, goal)
    return {"summary": summary, "deadline": deadline, "tickets": related}
