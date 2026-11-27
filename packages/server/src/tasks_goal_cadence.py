"""Select signed goal-review cues without changing ticket state or queue priority."""

from collections import Counter
from datetime import datetime, timedelta, timezone
import re

import tasks_core as ledger
import tasks_goals as goals
import tasks_projects as projects
import tasks_ring1 as ring1


SCHEMA = """
    CREATE TABLE IF NOT EXISTS goal_review_times (
        task_id INTEGER PRIMARY KEY REFERENCES task_goals (task_id),
        review_time TEXT NOT NULL
    )
"""
DEADLINE_TIME = "09:00"


INACTIVE_REVIEW = "goal-review-inactive:"
REVIEW_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")


def inactive_key(number):
    return f"{INACTIVE_REVIEW}{number}"


def schedule_rows(db, owner, project):
    return db.execute(
        """SELECT tasks.id, review.review_time, paused.value AS paused_time
           FROM tasks
           JOIN task_goals AS goal ON goal.task_id = tasks.id AND goal.retired = 0
           LEFT JOIN goal_review_times AS review ON review.task_id = tasks.id
           LEFT JOIN board_settings AS paused ON paused.key = ? || CAST(tasks.id AS TEXT)
           WHERE tasks.dri = ? AND (? IS NULL OR tasks.project_prefix = ?)
             AND (review.review_time IS NOT NULL OR paused.value IS NOT NULL)""",
        (INACTIVE_REVIEW, owner, project, project),
    )


def schedule_times(db, number):
    row = db.execute(
        """SELECT review.review_time, paused.value AS paused_time FROM tasks
           LEFT JOIN goal_review_times AS review ON review.task_id = tasks.id
           LEFT JOIN board_settings AS paused ON paused.key = ?
           WHERE tasks.id = ?""", (inactive_key(number), number),
    ).fetchone()
    return row["review_time"], row["paused_time"]


def save_review_time(db, owner, number, value):
    return change_schedule(db, owner, number, "cancel" if value == "" else "enable", value)


def change_schedule(db, owner, number, change, value=None):
    task = ledger.find_task(db, number)
    if task is None or task["dri"] != owner:
        raise PermissionError("Choose a goal you own.")
    if task["state"] == "done" or ledger.task_goal_kind(db, number) is None:
        raise ValueError("Choose an active goal ticket.")
    project = projects.row_project(task)
    current_time, paused_time = schedule_times(db, number)
    if change == "enable":
        if not isinstance(value, str) or not REVIEW_TIME.fullmatch(value):
            raise ValueError("Provide a Pacific review time in HH:MM.")
        next_time, next_paused = value, None
    elif change == "pause":
        next_time, next_paused = None, current_time or paused_time
    elif change == "resume":
        next_time = paused_time or current_time
        next_paused = None if next_time else paused_time
    else:
        # Keep an empty marker so cancellation does not reactivate a superseded review date.
        next_time = None
        next_paused = "" if current_time is not None or paused_time is not None else None

    # Older builds read only the active table, so a paused reminder stays silent on rollback.
    if next_time != current_time:
        if next_time is None:
            db.execute("DELETE FROM goal_review_times WHERE task_id = ?", (number,))
        else:
            db.execute(
                """INSERT INTO goal_review_times (task_id, review_time) VALUES (?, ?)
                   ON CONFLICT (task_id) DO UPDATE SET review_time = excluded.review_time""",
                (number, next_time),
            )
    if next_paused != paused_time:
        if next_paused is None:
            db.execute("DELETE FROM board_settings WHERE key = ?", (inactive_key(number),))
        else:
            db.execute(
                """INSERT INTO board_settings (key, value) VALUES (?, ?)
                   ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
                (inactive_key(number), next_paused),
            )
    if (next_time, next_paused) != (current_time, paused_time):
        ledger.bump_revision(db, project)
    return {"ticket": projects.row_reference(task), "review_time": next_time or next_paused or None,
            "state": "active" if next_time else "paused" if next_paused else None}


def completed_today(at, now):
    return at is not None and datetime.fromisoformat(at).astimezone(goals.PACIFIC).date() == now.date()


def describe_schedule(active_time, paused_time, last_completed, now):
    if not (active_time or paused_time):
        return None
    upcoming = None
    if active_time:
        day = now.date() + timedelta(days=1) if completed_today(last_completed, now) else now.date()
        upcoming = goals.review_slot(day, active_time).isoformat().replace("+00:00", "Z")
    return {"state": "active" if active_time else "paused", "review_time": active_time or paused_time,
            "cadence": "daily", "time_zone": "America/Los_Angeles", "next_at": upcoming}


def ticket_schedule(db, task, current_time, paused_time):
    number = task["id"]
    if current_time is None and paused_time is None:
        return None
    _, last_completed = review_history(db, [task]) if current_time else ({}, {})
    now = datetime.fromisoformat(ledger.timestamp()).astimezone(goals.PACIFIC)
    return describe_schedule(current_time, paused_time, last_completed.get(number), now)


def schedules(db, owner, project, now):
    active = goals.goal_rows(db, [owner], project)
    times = {row["id"]: row for row in schedule_rows(db, owner, project)}
    _, last_completed = review_history(db, [row for row in active if row["id"] in times])
    return {
        projects.row_reference(row): describe_schedule(
            saved["review_time"], saved["paused_time"], last_completed.get(row["id"]), now,
        )
        for row in active if (saved := times.get(row["id"])) is not None
        and (saved["review_time"] or saved["paused_time"])
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


def candidates(db, owner, project, now, *, busy, ordinary_busy, include_timed=False):
    """Read actor-owned cues. A consumer deduplicates each stable key across wakes."""
    today = now.date().isoformat()
    result = {
        "busy": busy, "ordinary_busy": ordinary_busy, "day": today,
        "standing": None, "cues": [], "drain_event": drain_event(db, owner),
    }
    if ordinary_busy:
        return result

    standing, cues = due_reviews(db, owner, project, now, principal=owner, include_timed=include_timed)
    if not busy or (standing and "continuation_event" in standing):
        result.update(standing=standing, cues=cues)
    return result


def continues_standing(row):
    return row["kind"] == "standing" and row["state"] == "in_progress" and bool(row["stopping_criterion"])


def due_reviews(db, owner, project, now, *, principal, include_timed=True):
    """Project the same due review for Inbox and the wake carrier."""
    today = now.date().isoformat()
    instant = now.astimezone(timezone.utc)
    active = [
        row for row in goals.goal_rows(db, [owner], project)
        if (row["state"] == "parked" or continues_standing(row))
        and ring1.visible(db, row["project_prefix"], principal)
    ]
    if not active:
        return None, []
    times = {row["id"]: row["review_time"] for row in schedule_rows(db, owner, project)}
    counts, last_completed = review_history(db, active)
    standing_offer = None
    deadlines, timed, daily = [], [], []
    for row in active:
        number = row["id"]
        ticket = projects.row_reference(row)
        summary = {"ticket": ticket, "summary": row["summary"], "completed_checks": counts[number]}
        standing = row["kind"] == "standing"
        if standing:
            if not goals.exploration_available(db, row, now):
                continue
            if row["stopping_criterion"]:
                summary["keep_exploring"] = True
            standing_offer = summary
            if continues_standing(row):
                summary["continuation_event"] = row["event_id"]
                continue
        if include_timed and (until := ledger.event_context(row).get("defer_until")):
            due = datetime.fromisoformat(until)
            if instant >= due:
                timed.append((due, {**summary, "kind": "timed", "key": f"timed:{ticket}:{until}", "day": today}))
                continue
        review_time = times.get(number)
        deadline = row["deadline"]
        if completed_today(last_completed.get(number), now):
            continue
        daily_due = review_time is not None and instant >= goals.review_slot(now.date(), review_time)
        deadline_due = deadline and (
            deadline < today or (deadline == today and instant >= goals.review_slot(now.date(), review_time or DEADLINE_TIME))
        )
        # The deadline and daily clock can describe the same review. Keep the deadline key
        # on its date and the daily key for later slots, including after a watcher restart.
        if deadline_due and (not daily_due or deadline == today):
            deadlines.append((deadline, {
                **summary, "kind": "deadline", "key": f"deadline:{ticket}:{deadline}", "day": today,
            }))
        elif daily_due:
            daily.append({
                **summary, "kind": "standing-daily" if standing else "daily", "key": f"daily:{ticket}:{today}", "day": today,
            })
    return standing_offer, ([cue for _, cue in sorted(deadlines, key=lambda item: item[0])]
                            + [cue for _, cue in sorted(timed, key=lambda item: item[0])] + daily)
