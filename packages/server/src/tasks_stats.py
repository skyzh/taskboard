"""Aggregate board-visible ledger records over one rolling UTC window."""

from collections import Counter
from datetime import datetime, timezone
from itertools import groupby
from math import ceil
import json

import tasks_profiles as profiles
import tasks_projects as projects
from tasks_core import current_tasks_sql, task_columns


PERIODS = {"hour": (12, 300), "day": (24, 3600), "week": (28, 21600)}
ROW_LIMIT = 25
OPEN_ACTIONS = {"start", "resume"}
BOUNDARIES = OPEN_ACTIONS | {"wait", "done", "park", "transfer", "assign-human"}
BOUNDARY_ACTIONS = ", ".join(repr(action) for action in sorted(BOUNDARIES))
BOUNDARY = f"""(action IN ({BOUNDARY_ACTIONS}) OR
                 (action = 'update' AND state = 'done' AND json_type(context, '$.closure') = 'object'))"""


def utc_text(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ledger_seconds(value):
    if value is None:
        return None
    try:
        at = datetime.fromisoformat(value)
        return int(at.timestamp()) if at.tzinfo is not None else None
    except (OverflowError, ValueError):
        return None


def activity_counts(db, bounds, base):
    totals = db.execute(
        """
        SELECT COUNT(*) AS events, COUNT(CASE WHEN action = 'done' THEN 1 END) AS completions,
               COUNT(CASE WHEN action = 'source-event' AND actor = 'TaskNote' THEN 1 END) AS tasknote_events
        FROM events WHERE at >= ? AND at < ?
        """, bounds,
    ).fetchone()
    columns = task_columns(db)
    project_prefix = "t.project_prefix" if "project_prefix" in columns else "'T'"
    project_number = "t.project_number" if "project_number" in columns else "NULL"
    tasks = db.execute(
        f"""
        WITH current AS ({current_tasks_sql(db)}), counts AS (
            SELECT task_id, COUNT(*) AS events, COUNT(CASE WHEN action = 'done' THEN 1 END) AS completions,
                   COUNT(CASE WHEN action = 'source-event' AND actor = 'TaskNote' THEN 1 END) AS tasknote_events
            FROM events WHERE at >= ? AND at < ? GROUP BY task_id
        )
        SELECT t.id, {project_prefix} AS project_prefix, {project_number} AS project_number,
               t.title, counts.events, counts.completions, counts.tasknote_events,
               current.state, current.display_state,
               (EXISTS (SELECT 1 FROM tasknote_snapshots n WHERE n.task_id = t.id) OR
                EXISTS (SELECT 1 FROM task_sources l JOIN sources s ON s.id = l.source_id
                        WHERE l.task_id = t.id AND s.kind = 'tasknote')) AS has_tasknote,
               COUNT(*) OVER () AS group_count
        FROM counts JOIN tasks t ON t.id = counts.task_id
        LEFT JOIN current ON current.id = t.id
        ORDER BY counts.events DESC, t.id
        LIMIT ?
        """, (*bounds, ROW_LIMIT),
    ).fetchall()
    agents = db.execute(
        """
        WITH counts AS (
            SELECT e.actor, COUNT(*) AS events, COUNT(CASE WHEN e.action = 'done' THEN 1 END) AS completions
            FROM events e JOIN board_principals p ON p.ledger_key = e.actor AND p.kind = 'agent'
            WHERE e.at >= ? AND e.at < ? GROUP BY e.actor
        )
        SELECT counts.actor, COALESCE(NULLIF(p.name, ''), NULLIF(p.username, ''), p.ledger_key) AS name,
               counts.events, counts.completions,
               COUNT(*) OVER () AS group_count, SUM(counts.events) OVER () AS event_count
        FROM counts JOIN board_principals p ON p.ledger_key = counts.actor
        ORDER BY counts.events DESC, name, counts.actor
        LIMIT ?
        """, (*bounds, ROW_LIMIT),
    ).fetchall()
    agent_profiles = profiles.visible_by_keys(db, (row["actor"] for row in agents), base)
    return {
        **dict(totals),
        "tasks": [
            {
                **{key: row[key] for key in ("id", "title", "state", "events", "completions", "tasknote_events", "has_tasknote")},
                **({"display_state": "closed"} if row["display_state"] == "closed" else {}),
                "key": projects.row_reference(row),
            }
            for row in tasks
        ],
        "task_count": tasks[0]["group_count"] if tasks else 0,
        "agents": [{
            "name": row["name"], "events": row["events"], "completions": row["completions"],
            "avatar_url": agent_profiles[row["actor"]]["avatar_url"],
        } for row in agents],
        "agent_count": agents[0]["group_count"] if agents else 0,
        "other_events": totals["events"] - (agents[0]["event_count"] if agents else 0),
    }


def dri_intervals(db, bounds, end):
    # Carry the last boundary before the window forward. Progress and logs
    # cannot establish a start, and transfer closes the former owner's interval.
    rows = db.execute(
        f"""
        WITH seed AS (
            SELECT MAX(id) AS id FROM events
            WHERE {BOUNDARY} AND at < ? GROUP BY task_id
        )
        SELECT task_id, action, actor, at, id, state FROM events JOIN seed USING (id)
        UNION ALL
        SELECT task_id, action, actor, at, id, state FROM events
        WHERE {BOUNDARY} AND at >= ? AND at < ?
        ORDER BY task_id, id
        """, (bounds[0], *bounds),
    )
    for _, history in groupby(rows, key=lambda row: row["task_id"]):
        opened = None
        for event in history:
            at = ledger_seconds(event["at"])
            if at is None:
                opened = None
                continue
            if opened is not None and opened[1] < at:
                yield (*opened, at)
            if event["action"] in OPEN_ACTIONS and event["state"] == "in_progress" and event["actor"]:
                opened = (event["actor"], at)
            else:
                opened = None
        if opened is not None and opened[1] < end:
            yield (*opened, end)


def worker_intervals(db, bounds, first, end):
    intervals = []
    coverage = {"measured_runs": 0, "unclosed_runs": 0, "invalid_runs": 0}
    rows = db.execute(
        """
        WITH timings AS (
            SELECT worker, started_at,
                   CASE WHEN status IN ('reported', 'collected') THEN reported_at
                        WHEN status = 'stopped' THEN finalized_at END AS ended_at
            FROM worker_runs
            WHERE started_at < ?
        )
        SELECT * FROM timings WHERE ended_at IS NULL OR started_at >= ? OR ended_at > ?
        """, (bounds[1], bounds[0], bounds[0]),
    )
    elapsed = 0
    for row in rows:
        since = ledger_seconds(row["started_at"])
        until = ledger_seconds(row["ended_at"])
        if since is None or not row["worker"] or (until is not None and until < since):
            coverage["invalid_runs"] += 1
        elif row["ended_at"] is not None and until is None:
            coverage["invalid_runs"] += 1
        elif until is None or until > end:
            coverage["unclosed_runs"] += 1
        elif until > first and since < end:
            start = max(since, first)
            if start < until:
                intervals.append((row["worker"], start, until))
                coverage["measured_runs"] += 1
                elapsed += until - start
    return intervals, {"elapsed_seconds": elapsed, **coverage}


def simultaneous_peak(points):
    active = Counter()
    peak = 0
    # Negative changes (ends) sort before positive changes (starts).
    for _, change, identity in sorted(points):
        active[identity] += change
        if not active[identity]:
            del active[identity]
        peak = max(peak, len(active))
    return peak


def bucket_peaks(intervals, first, width, count):
    buckets = [[] for _ in range(count)]
    for identity, start, end in intervals:
        left = max(0, (start - first) // width)
        right = min(count, (end - first + width - 1) // width)
        for index in range(left, right):
            lower = first + index * width
            upper = lower + width
            buckets[index].extend(((max(start, lower), 1, identity), (min(end, upper), -1, identity)))
    return [simultaneous_peak(bucket) for bucket in buckets]

def snapshot(db, period, now=None, *, base="", watcher_task=None):
    count, width = PERIODS[period]
    # Ledger timestamps have whole-second precision. Keep the upper bound
    # exclusive without dropping events from the current partial second.
    end = ceil((now or datetime.now(timezone.utc)).timestamp())
    first = end - count * width
    bounds = (utc_text(first), utc_text(end))
    active = bucket_peaks(dri_intervals(db, bounds, end), first, width, count)
    worker_runs, workers = worker_intervals(db, bounds, first, end)
    worker_counts = bucket_peaks(worker_runs, first, width, count)
    measurements = db.execute(
        """
        SELECT json_extract(context, '$.watcher_measurements')
        FROM events
        WHERE task_id = ? AND action = 'log' AND json_type(context, '$.watcher_measurements') = 'object'
        ORDER BY id DESC LIMIT 1
        """, (watcher_task,),
    ).fetchone()
    return {
        "period": period, "from": bounds[0], "to": bounds[1], "bucket_seconds": width,
        "activity": activity_counts(db, bounds, base), "worker_hours": workers,
        **({"watcher_measurements": json.loads(measurements[0])} if measurements else {}),
        "buckets": [
            {"from": utc_text(first + index * width), "to": utc_text(first + (index + 1) * width),
             "dris": active[index], "workers": worker_counts[index]}
            for index in range(count)
        ],
    }
