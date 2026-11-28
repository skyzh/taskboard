"""Anchored interval schedules for goal Review occurrences."""

from datetime import datetime, timedelta, timezone
import json

from tasks_recurring import utc_text


PREFIX = "goal-review-interval:"


def read(db, number):
    row = db.execute("SELECT value FROM board_settings WHERE key = ?", (PREFIX + str(number),)).fetchone()
    return json.loads(row["value"]) if row else None


def write(db, number, schedule):
    key = PREFIX + str(number)
    if schedule is None:
        db.execute("DELETE FROM board_settings WHERE key = ?", (key,))
    else:
        db.execute(
            """INSERT INTO board_settings (key, value) VALUES (?, ?)
               ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
            (key, json.dumps(schedule, separators=(",", ":"), sort_keys=True)),
        )


def period(schedule):
    return timedelta(hours=schedule["cadence_hours"])


def anchor(schedule):
    return datetime.fromisoformat(schedule["anchor_at"])


def next_after(schedule, at):
    """Return the first anchor-aligned slot strictly after at."""
    first = anchor(schedule)
    if at < first:
        return first
    step = period(schedule)
    return first + ((at - first) // step + 1) * step


def current_slot(schedule, now):
    """Coalesce missed intervals to the latest slot at or before now."""
    if schedule["paused"]:
        return None
    first = anchor(schedule)
    current = now.astimezone(timezone.utc)
    if current < first:
        return None
    step = period(schedule)
    slot = first + ((current - first) // step) * step
    resumed = schedule.get("resumed_at")
    return None if resumed and slot < datetime.fromisoformat(resumed) else slot


def occurrence_key(ticket, slot):
    return f"review:{ticket}:{utc_text(slot)}"
