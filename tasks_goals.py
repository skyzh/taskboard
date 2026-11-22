"""A project assignee's dated and standing goals."""

import json

import tasks_projects as projects


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


def ensure_extended_schema(db):
    db.execute(STANDING_SCHEMA)
    db.execute(MILESTONE_SCHEMA)


def standing(db, owner, project=None):
    rows = db.execute(
        """
        SELECT project, summary FROM board_standing_goals
        WHERE owner = ? AND (? IS NULL OR project = ?) ORDER BY project
        """, (owner, project, project),
    )
    return [dict(row) for row in rows]


def save_standing(db, project, owner, summary):
    if summary is None:
        db.execute("DELETE FROM board_standing_goals WHERE project = ? AND owner = ?", (project, owner))
        return None
    db.execute(
        """
        INSERT INTO board_standing_goals (project, owner, summary) VALUES (?, ?, ?)
        ON CONFLICT (project, owner) DO UPDATE SET summary = excluded.summary
        """, (project, owner, summary),
    )
    return {"project": project, "summary": summary}


def report_summaries(db, project, owners):
    if not owners:
        return {}
    placeholders = ", ".join("?" for _ in owners)
    scope = f"WHERE project = ? AND owner IN ({placeholders})"
    summaries = {}
    for kind, table, deadline in (
        ("standing", "board_standing_goals", "NULL AS deadline"),
        ("original", "board_project_goals", "deadline"),
        ("milestone", "board_goal_milestones", "deadline"),
    ):
        rows = db.execute(
            f"SELECT owner, summary, {deadline} FROM {table} {scope} ORDER BY deadline, summary",
            (project, *owners),
        )
        for row in rows:
            summaries.setdefault(row["owner"], []).append({
                "kind": kind, "summary": row["summary"], "deadline": row["deadline"],
            })
    return summaries


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
    row = db.execute(
        "SELECT summary, deadline, ticket_ids FROM board_project_goals WHERE project = ? AND owner = ?",
        (project, owner),
    ).fetchone()
    if row is None:
        return None
    return dated_goal(db, project, owner, row)


def dated_goal(db, project, owner, row):
    return {
        "summary": row["summary"],
        "deadline": row["deadline"],
        "tickets": tickets(db, project, owner, json.loads(row["ticket_ids"])),
    }


def save(db, project, owner, goal):
    if goal is None:
        db.execute("DELETE FROM board_project_goals WHERE project = ? AND owner = ?", (project, owner))
        return None

    summary, deadline, ids = goal
    related = owned_tickets(db, project, owner, ids)
    db.execute(
        """
        INSERT INTO board_project_goals (project, owner, summary, deadline, ticket_ids)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (project, owner) DO UPDATE SET
            summary = excluded.summary, deadline = excluded.deadline, ticket_ids = excluded.ticket_ids
        """,
        (project, owner, summary, deadline, json.dumps(ids)),
    )
    return {"summary": summary, "deadline": deadline, "tickets": related}


def owned_tickets(db, project, owner, ids):
    related = tickets(db, project, owner, ids)
    if len(related) != len(ids):
        raise ValueError("Choose tickets you own in this project.")
    return related


def milestones(db, project, owner):
    rows = db.execute(
        """
        SELECT summary, deadline, ticket_ids FROM board_goal_milestones
        WHERE project = ? AND owner = ? ORDER BY deadline, summary
        """, (project, owner),
    ).fetchall()
    return [dated_goal(db, project, owner, row) for row in rows]


def save_milestone(db, project, owner, previous, goal):
    if goal is None:
        db.execute("DELETE FROM board_goal_milestones WHERE project = ? AND owner = ? AND summary = ?",
                   (project, owner, previous))
        return None
    summary, deadline, ids = goal
    related = owned_tickets(db, project, owner, ids)
    encoded = json.dumps(ids)
    if previous is None:
        result = db.execute(
            """
            INSERT INTO board_goal_milestones (project, owner, summary, deadline, ticket_ids)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (project, owner, summary) DO NOTHING
            """, (project, owner, summary, deadline, encoded),
        )
        if not result.rowcount:
            raise ValueError("A milestone with this name already exists. Edit it or choose a different name.")
    else:
        if previous != summary and db.execute(
            "SELECT 1 FROM board_goal_milestones WHERE project = ? AND owner = ? AND summary = ?",
            (project, owner, summary),
        ).fetchone():
            raise ValueError("Choose a different milestone name.")
        result = db.execute(
            """
            UPDATE board_goal_milestones SET summary = ?, deadline = ?, ticket_ids = ?
            WHERE project = ? AND owner = ? AND summary = ?
            """, (summary, deadline, encoded, project, owner, previous),
        )
        if not result.rowcount:
            raise LookupError("Milestone not found in your goals.")
    return {"summary": summary, "deadline": deadline, "tickets": related}
