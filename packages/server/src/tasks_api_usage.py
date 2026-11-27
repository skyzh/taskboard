"""Daily request counts for signed agents and their declared work tickets."""

from datetime import date

from taskboard_client.connection import WORK_TICKET_HEADER

import tasks_projects as projects
import tasks_ring1 as ring1


TABLE = "agent_api_usage"
COLUMNS = {"day", "actor", "category", "project", "ticket", "calls"}
SCHEMA = f"""CREATE TABLE {TABLE} (
    day TEXT NOT NULL,
    actor TEXT NOT NULL,
    category TEXT NOT NULL,
    project TEXT NOT NULL,
    ticket BIGINT NOT NULL,
    calls BIGINT NOT NULL,
    PRIMARY KEY (day, actor, category, project, ticket)
)"""
HEADER = WORK_TICKET_HEADER
TOTAL = 0
UNATTRIBUTED = -1


def references(value):
    if not isinstance(value, str) or len(value) > 2048:
        return ()
    result = {}
    for part in value.split(",")[:32]:
        try:
            reference = projects.identifier(part.strip())
        except ValueError:
            continue
        result.setdefault(reference, None)
    return tuple(result)


def eligible_tickets(db, actor, declared):
    result = {}
    for project, number in declared:
        if not ring1.visible(db, project, actor):
            continue
        task = projects.find_reference(db, project, number, migrated=True)
        if task is None:
            continue
        if task["project_prefix"] != project and not ring1.visible(db, task["project_prefix"], actor):
            continue
        work = db.execute("SELECT 1 FROM tasks WHERE id = ? AND record_kind = 'work'", (task["id"],)).fetchone()
        if work is not None:
            result[task["id"]] = task["project_prefix"]
    return result


def record(db, day, actor, category, declared):
    tickets = eligible_tickets(db, actor, declared)
    rows = [("", TOTAL), *((project, number) for number, project in tickets.items())]
    if not tickets:
        rows.append(("", UNATTRIBUTED))
    for project, ticket in rows:
        db.execute(
            f"""INSERT INTO {TABLE} (day, actor, category, project, ticket, calls)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT (day, actor, category, project, ticket)
                DO UPDATE SET calls = {TABLE}.calls + 1""",
            (day, actor, category, project, ticket),
        )


def report(db, day: date, viewer, project=None, *, project_manager=False):
    counts = []
    if project is None:
        counts = [{
            "actor": row["actor"], "category": row["category"],
            "scope": "agent" if row["ticket"] == TOTAL else "unattributed",
            "calls": row["calls"],
        } for row in db.execute(
            f"""SELECT actor, category, ticket, calls FROM {TABLE}
                WHERE day = ? AND actor = ? AND ticket <= 0
                ORDER BY category, ticket DESC""", (day.isoformat(), viewer),
        )]
    original_visibility, original_bindings = ring1.visibility(db, "usage.project", viewer)
    current_visibility, current_bindings = ring1.visibility(db, "tasks.project_prefix", viewer)
    by_ticket = [
        {"actor": row["actor"], "category": row["category"], "scope": "ticket",
         "ticket": projects.row_reference(row), "calls": int(row["calls"])}
        for row in db.execute(
            f"""SELECT usage.actor, usage.category, SUM(usage.calls) AS calls,
                       tasks.id, tasks.project_prefix, tasks.project_number
                FROM {TABLE} AS usage JOIN tasks ON tasks.id = usage.ticket
                WHERE usage.day = ? AND (? OR usage.actor = ?)
                  AND (? IS NULL OR tasks.project_prefix = ?)
                  AND {original_visibility} AND {current_visibility}
                GROUP BY usage.actor, usage.category, tasks.id, tasks.project_prefix, tasks.project_number
                ORDER BY usage.actor, tasks.id, usage.category""",
            (day.isoformat(), project_manager, viewer, project, project, *original_bindings, *current_bindings),
        )
    ]
    return {"day": day.isoformat(), "time_zone": "America/Los_Angeles", "items": counts + by_ticket}
