"""Collect actionable issues from a project's current tickets."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import tasks_core as ledger
import tasks_projects as projects
import tasks_recurring as recurring
import watches
import tasks_storage


def open_attention_waits(db, current_tasks, project):
    # Only a current, board-visible Needs me wait signals a request in the report.
    # Unlinked requests stay in their private recipient feeds.
    columns = tasks_storage.table_columns(db, "attention_requests")
    if not columns or not watches.has_table(db, "attention_wait_links"):
        return {}
    remind_on = "requests.remind_on" if "remind_on" in columns else "NULL"
    return {
        row["id"]: row for row in db.execute(
            f"""
            WITH current_tasks AS ({current_tasks})
            SELECT current_tasks.id, requests.raised_at, {remind_on} AS remind_on
            FROM current_tasks
            JOIN attention_wait_links AS waits ON waits.event_id = current_tasks.event_id
            JOIN attention_requests AS requests ON requests.id = waits.request_id
            WHERE current_tasks.project_prefix = ?
              AND current_tasks.state = 'waiting' AND current_tasks.waiting_on = 'Needs me'
              AND requests.handled_at IS NULL
            """, (project,),
        )
    }


def build(db, project, as_of, cadence_hours, members):
    """Return a report brief and source ticket links for a maintenance occurrence."""
    now = recurring.utc(as_of)
    cadence = timedelta(hours=cadence_hours)
    current_tasks = ledger.current_tasks_sql(db)
    rows = db.execute(
        f"""
        SELECT id, project_prefix, project_number, dri, state, event_id, at, eta
        FROM ({current_tasks})
        WHERE project_prefix = ? AND state IN ('ready', 'in_progress', 'waiting', 'human_review')
        ORDER BY id
        """, (project,),
    ).fetchall()
    waits = [row for row in rows if row["state"] == "waiting"]
    wait_starts = {}
    # The wait reader binds two SQLite parameters per task.
    for offset in range(0, len(waits), 400):
        wait_starts.update(ledger.wait_starts(db, waits[offset:offset + 400]))
    attention = open_attention_waits(db, current_tasks, project) if waits else {}
    today = now.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat() if attention else None

    def old_enough(at):
        return now - datetime.fromisoformat(at) >= cadence

    sections = {
        "Unassigned work": [],
        "Owners outside the project roster": [],
        "Stale waits": [],
        "Overdue requests": [],
    }
    sources = {}
    base = ledger.live_board_url(db)
    for row in rows:
        ref = projects.row_reference(row)
        number, owner = row["id"], row["dri"]
        categories = []
        if not owner:
            categories.append("Unassigned work")
        elif members is not None and owner not in members:
            categories.append("Owners outside the project roster")

        request = attention.get(number)
        deferred = request and request["remind_on"] and request["remind_on"] > today
        wait_at = wait_starts.get(number)
        if wait_at and not deferred and old_enough(wait_at):
            categories.append("Stale waits")
        if row["state"] == "human_review":
            overdue = now >= ledger.parse_eta(row["eta"]) if row["eta"] else old_enough(row["at"])
        else:
            overdue = request and not deferred and old_enough(request["raised_at"])
        if overdue:
            categories.append("Overdue requests")

        for category in categories:
            sections[category].append(ref)
        if categories and base:
            sources[ref] = f"{base}#task-{projects.anchor(row)}"

    lines = [f"- {label} — {', '.join(refs)}" for label, refs in sections.items() if refs]
    if not lines:
        lines = ["No maintenance issues found in this project."]
    if members is None:
        lines.extend(("", "Set a project roster to check whether task owners belong to the project."))
    return "\n".join(lines), list(sources.items())
