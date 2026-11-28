"""Select signed goal-review cues without changing ticket state or queue priority."""

from collections import Counter
from datetime import datetime, timedelta, timezone
import re

import tasks_core as ledger
import tasks_goals as goals
import tasks_goal_intervals as intervals
import tasks_projects as projects
import tasks_ring1 as ring1
from tasks_recurring import checked_slot, utc, utc_text


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


def change_schedule(db, owner, number, change, value=None, *, cadence_hours=None, next_at=None):
    task = ledger.find_task(db, number)
    if task is None or task["dri"] != owner:
        raise PermissionError("Choose a goal you own.")
    if task["state"] == "done" or ledger.task_goal_kind(db, number) is None:
        raise ValueError("Choose an active goal ticket.")
    project = projects.row_project(task)
    current_time, paused_time = schedule_times(db, number)
    current_interval = intervals.read(db, number)
    next_interval = current_interval
    if change == "enable":
        if cadence_hours is not None or next_at is not None:
            if value is not None or type(cadence_hours) is not int or cadence_hours <= 0 or next_at is None:
                raise ValueError("Choose a daily Pacific time or a positive interval and its first Review time.")
            start = utc(next_at)
            checked_slot(start, cadence_hours)
            next_interval = {"cadence_hours": cadence_hours, "anchor_at": utc_text(start), "paused": False}
            if current_interval and not current_interval["paused"] and all(
                next_interval[field] == current_interval[field] for field in next_interval
            ):
                next_interval = current_interval
            next_time, next_paused = None, ""
        else:
            if not isinstance(value, str) or not REVIEW_TIME.fullmatch(value):
                raise ValueError("Provide a Pacific review time in HH:MM.")
            next_interval = None
            next_time, next_paused = value, None
    elif current_interval is not None:
        next_time, next_paused = None, ""
        if change == "pause":
            next_interval = {**current_interval, "paused": True}
        elif change == "resume":
            if current_interval["paused"]:
                next_interval = {**current_interval, "paused": False, "resumed_at": ledger.timestamp()}
        else:
            next_interval = None
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
    if next_interval != current_interval:
        intervals.write(db, number, next_interval)
    if (next_time, next_paused, next_interval) != (current_time, paused_time, current_interval):
        ledger.bump_revision(db, project)
    ticket = projects.row_reference(task)
    if next_interval is not None:
        now = datetime.fromisoformat(ledger.timestamp())
        return {"ticket": ticket, "review_time": None, **describe_interval(db, task, next_interval, now)}
    return {"ticket": ticket, "review_time": next_time or next_paused or None,
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


def describe_interval(db, task, schedule, now):
    upcoming = None
    if not schedule["paused"]:
        slot = intervals.current_slot(schedule, now)
        if slot is None or occurrence_receipt(db, task["id"], intervals.occurrence_key(projects.row_reference(task), slot)):
            slot = intervals.next_after(schedule, now)
        upcoming = utc_text(slot)
    return {"state": "paused" if schedule["paused"] else "active", "cadence": "interval",
            "cadence_hours": schedule["cadence_hours"], "anchor_at": schedule["anchor_at"], "next_at": upcoming}


def ticket_schedule(db, task, current_time, paused_time, *, now=None, last_completed=None):
    number = task["id"]
    interval = intervals.read(db, number)
    if interval is None and current_time is None and paused_time is None:
        return None
    supplied_time = now is not None
    now = now or datetime.fromisoformat(ledger.timestamp()).astimezone(goals.PACIFIC)
    if interval is not None:
        return describe_interval(db, task, interval, now)
    if not supplied_time and current_time:
        _, history = review_history(db, [task])
        last_completed = history.get(number)
    return describe_schedule(current_time, paused_time, last_completed, now)


def schedules(db, owner, project, now):
    active = goals.goal_rows(db, [owner], project)
    times = {row["id"]: row for row in schedule_rows(db, owner, project)}
    _, last_completed = review_history(db, [row for row in active if row["id"] in times])
    result = {}
    for row in active:
        saved = times.get(row["id"])
        description = ticket_schedule(
            db, row, saved["review_time"] if saved else None, saved["paused_time"] if saved else None,
            now=now, last_completed=last_completed.get(row["id"]),
        )
        if description:
            result[projects.row_reference(row)] = description
    return result


def occurrence_receipt(db, number, occurrence_key):
    return db.execute(
        """SELECT id, at FROM events WHERE task_id = ? AND action = 'log'
           AND json_extract(context, '$.goal_occurrence') = ?
           ORDER BY id LIMIT 1""",
        (number, occurrence_key),
    ).fetchone()


def occurrences(db, owner, project, now):
    """Offer daily Explore and scheduled Review independently on the same goal."""
    slots = list(_occurrence_slots(db, owner, project, now))
    # Fill the existing Explore slots with first passes, then least recently
    # completed repeats. A delivery receipt does not count as a completed pass.
    explores = iter(sorted((slot for slot in slots if slot[1]["kind"] == "explore"), key=lambda slot: slot[0]))
    for _, offer in slots:
        yield next(explores)[1] if offer["kind"] == "explore" else offer


def _occurrence_slots(db, owner, project, now):
    daily = {row["id"]: row["review_time"] for row in schedule_rows(db, owner, project)}
    day = now.date().isoformat()
    for row in goals.goal_rows(db, [owner], project):
        number = row["id"]
        review_time = daily.get(number)
        if row["state"] == "closed" or not ring1.visible(db, row["project_prefix"], owner):
            continue
        ticket = projects.row_reference(row)
        deferred_until = ledger.event_context(row).get("defer_until")
        if (row["state"] in ("parked", "ready", "in_progress")
                and (not deferred_until or now >= datetime.fromisoformat(deferred_until))
                and goals.exploration_available(db, row, now)):
            key = f"explore:{ticket}:{day}"
            completed = occurrence_receipt(db, number, key)
            if completed is None:
                yield 0, {"kind": "explore", "ticket": ticket, "key": key, "day": day}
            elif row["stopping_criterion"] is not None:
                last = db.execute(
                    """SELECT id FROM events WHERE task_id = ? AND action = 'log'
                       AND json_extract(context, '$.goal_occurrence') LIKE ?
                       ORDER BY id DESC LIMIT 1""",
                    (number, key + ":%"),
                ).fetchone()
                last_completed = (last or completed)["id"]
                yield last_completed, {"kind": "explore", "ticket": ticket, "key": f"{key}:{last_completed}",
                                       "day": day}
        if review_time:
            due_at = goals.review_slot(now.date(), review_time)
            if now < due_at:
                continue
            key = f"review:{ticket}:{day}"
        else:
            schedule = intervals.read(db, number)
            due_at = intervals.current_slot(schedule, now) if schedule else None
            if due_at is None:
                continue
            key = intervals.occurrence_key(ticket, due_at)
        if occurrence_receipt(db, number, key) is None:
            yield 0, {"kind": "review", "ticket": ticket, "key": key, "day": day,
                      "due_at": due_at.isoformat().replace("+00:00", "Z")}


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
            WHERE task_id IN ({placeholders}) AND
              (action NOT IN ('source-event', 'log') OR json_extract(context, '$.goal_occurrence') IS NOT NULL)
            ORDER BY task_id, events.id""",
        [row["id"] for row in active],
    )
    counts, last_completed = Counter(), {}
    for event, review in goals.review_events(rows):
        if review == "completed":
            counts[event["id"]] += 1
            key = ledger.event_context(event).get("goal_occurrence", "")
            # Fixed intervals have a UTC time in the key. Their outcomes do not consume a daily slot.
            if event["action"] != "log" or key.count(":") == 2:
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
    deadlines, timed, scheduled = [], [], []
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
        interval = intervals.read(db, number)
        if interval and (slot := intervals.current_slot(interval, now)) is not None:
            key = intervals.occurrence_key(ticket, slot)
            if occurrence_receipt(db, number, key) is None:
                scheduled.append({**summary, "kind": "interval", "key": key, "day": today})
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
            scheduled.append({
                **summary, "kind": "standing-daily" if standing else "daily", "key": f"daily:{ticket}:{today}", "day": today,
            })
    return standing_offer, ([cue for _, cue in sorted(deadlines, key=lambda item: item[0])]
                            + [cue for _, cue in sorted(timed, key=lambda item: item[0])] + scheduled)
