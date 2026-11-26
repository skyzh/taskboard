"""Select signed goal-review cues without changing ticket state or queue priority."""

from collections import Counter
import re

import tasks_core as ledger
import tasks_goals as goals
import tasks_projects as projects
from tasks_queries import pacific_day


SCHEMA = """
    CREATE TABLE IF NOT EXISTS goal_review_times (
        task_id INTEGER PRIMARY KEY REFERENCES task_goals (task_id),
        review_time TEXT NOT NULL
    )
"""
DEADLINE_TIME = "09:00"


def save_review_time(db, owner, number, value):
    if not isinstance(value, str) or (value != "" and not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value)):
        raise ValueError("Provide a Pacific review time in HH:MM or an empty value to clear it.")
    task = ledger.find_task(db, number)
    if task is None or task["dri"] != owner:
        raise PermissionError("Choose a goal you own.")
    if task["state"] == "done" or ledger.task_goal_kind(db, number) is None:
        raise ValueError("Choose an active goal ticket.")
    if value == "":
        changed = db.execute("DELETE FROM goal_review_times WHERE task_id = ?", (number,)).rowcount
    else:
        changed = db.execute(
            """INSERT INTO goal_review_times (task_id, review_time) VALUES (?, ?)
               ON CONFLICT (task_id) DO UPDATE SET review_time = excluded.review_time
               WHERE goal_review_times.review_time <> excluded.review_time""",
            (number, value),
        ).rowcount
    if changed:
        ledger.bump_revision(db, projects.row_project(task))
    return {"ticket": projects.row_reference(task), "review_time": value or None}


def review_times(db, owner, project):
    active = {row["id"]: projects.row_reference(row) for row in goals.goal_rows(db, [owner], project)}
    return {
        active[row["task_id"]]: row["review_time"]
        for row in db.execute(
            """SELECT schedule.task_id, schedule.review_time
               FROM goal_review_times AS schedule JOIN tasks ON tasks.id = schedule.task_id
               WHERE tasks.dri = ? AND tasks.project_prefix = ?""",
            (owner, project),
        ) if row["task_id"] in active
    }


def drain_event(db, owner):
    """Return the latest ordinary-work disposition recorded by the signed owner."""
    ordinary = db.execute(
        """SELECT e.id
           FROM events AS e
           JOIN events AS previous ON previous.id = (
               SELECT id FROM events
               WHERE task_id = e.task_id AND id < e.id AND action NOT IN ('source-event', 'log')
               ORDER BY id DESC LIMIT 1
           )
           WHERE e.actor = ? AND previous.state IN ('ready', 'in_progress')
             AND (e.action IN ('done', 'park', 'wait', 'transfer', 'assign-human')
                  OR (e.action = 'update' AND e.state = 'done'))
             AND (e.action <> 'transfer' OR COALESCE(json_extract(e.context, '$.previous_dri'), e.actor) = e.actor)
             AND (e.action <> 'update' OR EXISTS (SELECT 1 FROM tasks WHERE id = e.task_id AND dri = e.actor))
             AND NOT EXISTS (SELECT 1 FROM task_goals WHERE task_id = e.task_id)
           ORDER BY e.id DESC LIMIT 1""",
        (owner,),
    ).fetchone()
    worker = db.execute(
        """SELECT e.id
           FROM events AS e
           JOIN worker_runs AS run ON run.task_id = e.task_id
           WHERE e.actor = ? AND e.action = 'log'
             AND NOT EXISTS (SELECT 1 FROM peer_asks WHERE review_run_id = run.id)
             AND (
                 (run.id = json_extract(e.context, '$.worker_report_id') AND run.worker = e.actor
                  AND run.role = 'review' AND run.status IN ('reported', 'collected'))
                 OR (run.id = json_extract(e.context, '$.worker_disposition_id')
                     AND run.status = 'collected')
             )
           ORDER BY e.id DESC LIMIT 1""",
        (owner,),
    ).fetchone()
    return max(ordinary["id"] if ordinary else 0, worker["id"] if worker else 0)


def review_history(db, active):
    if not active:
        return Counter(), {}
    placeholders = ", ".join("?" for _ in active)
    rows = db.execute(
        f"""SELECT task_id AS id, action, state, at, note, links, context FROM events
            WHERE task_id IN ({placeholders}) AND action NOT IN ('source-event', 'log')
            ORDER BY task_id, events.id""",
        [row["id"] for row in active],
    )
    counts, last_completed = Counter(), {}
    for event, review in goals.review_events(rows):
        if review == "completed":
            counts[event["id"]] += 1
            last_completed[event["id"]] = event["at"]
    return counts, last_completed


def candidates(db, owner, project, now, *, busy, ordinary_busy):
    """Read actor-owned cues. A consumer deduplicates each stable key across wakes."""
    today, clock = now.date().isoformat(), now.strftime("%H:%M")
    result = {
        "busy": busy, "ordinary_busy": ordinary_busy, "day": today,
        "standing": None, "cues": [], "drain_event": drain_event(db, owner),
    }
    if busy:
        return result

    active = [row for row in goals.goal_rows(db, [owner], project) if row["state"] == "parked"]
    times = review_times(db, owner, project)
    counts, last_completed = review_history(db, active)
    deadlines, daily = [], []
    for row in active:
        number = row["id"]
        ticket = projects.row_reference(row)
        summary = {"ticket": ticket, "summary": row["summary"], "completed_checks": counts[number]}
        standing = row["kind"] == "standing"
        if standing:
            result["standing"] = summary
        review_time = times.get(ticket)
        deadline = row["deadline"]
        if deadline and (deadline < today or (deadline == today and clock >= (review_time or DEADLINE_TIME))):
            deadlines.append((deadline, {
                **summary, "kind": "deadline", "key": f"deadline:{ticket}:{deadline}", "day": today,
            }))
        reviewed_today = number in last_completed and pacific_day(last_completed[number]) == today
        if review_time and clock >= review_time and not reviewed_today:
            daily.append({
                **summary, "kind": "standing-daily" if standing else "daily", "key": f"daily:{ticket}:{today}", "day": today,
            })
    result["cues"] = [cue for _, cue in sorted(deadlines, key=lambda item: item[0])] + daily
    return result
