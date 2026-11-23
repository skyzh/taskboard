"""Aggregate board-visible ledger records over one rolling UTC window."""

from collections import Counter
from datetime import datetime, timezone
from itertools import groupby
from math import ceil
from statistics import median
import json

import tasks_profiles as profiles
import tasks_projects as projects
import tasks_storage
from tasks_core import current_tasks_sql, has_event_context, task_columns


PERIODS = {"hour": (12, 300), "day": (24, 3600), "week": (28, 21600)}
ROW_LIMIT = 25
OPEN_ACTIONS = {"start", "resume"}
BOUNDARIES = OPEN_ACTIONS | {"wait", "done", "park", "transfer", "assign-human"}
BOUNDARY_ACTIONS = ", ".join(repr(action) for action in sorted(BOUNDARIES))


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


def task_scope(db, project):
    columns = task_columns(db)
    prefix = "project_prefix" if "project_prefix" in columns else "'T'"
    number = "project_number" if "project_number" in columns else "NULL"
    query = f"SELECT id, title, {prefix} AS project_prefix, {number} AS project_number FROM tasks"
    terms = ["record_kind = 'work'"] if "record_kind" in columns else []
    if project is not None:
        terms.append(f"{prefix} = ?")
    where = " WHERE " + " AND ".join(terms) if terms else ""
    return query + where, (project,) if project is not None else ()


def activity_counts(db, bounds, base, scope):
    selection, params = scope
    totals = db.execute(
        f"""
        WITH selected AS ({selection})
        SELECT COUNT(*) AS events, COUNT(CASE WHEN action = 'done' THEN 1 END) AS completions,
               COUNT(CASE WHEN action = 'source-event' AND actor = 'TaskNote' THEN 1 END) AS tasknote_events
        FROM events JOIN selected ON selected.id = events.task_id WHERE at >= ? AND at < ?
        """, (*params, *bounds),
    ).fetchone()
    tasks = db.execute(
        f"""
        WITH selected AS ({selection}), current AS ({current_tasks_sql(db)}), counts AS (
            SELECT task_id, COUNT(*) AS events, COUNT(CASE WHEN action = 'done' THEN 1 END) AS completions,
                   COUNT(CASE WHEN action = 'source-event' AND actor = 'TaskNote' THEN 1 END) AS tasknote_events
            FROM events JOIN selected ON selected.id = events.task_id
            WHERE at >= ? AND at < ? GROUP BY task_id
        )
        SELECT t.id, t.project_prefix, t.project_number,
               t.title, counts.events, counts.completions, counts.tasknote_events,
               current.state, current.display_state,
               (EXISTS (SELECT 1 FROM tasknote_snapshots n WHERE n.task_id = t.id) OR
                EXISTS (SELECT 1 FROM task_sources l JOIN sources s ON s.id = l.source_id
                        WHERE l.task_id = t.id AND s.kind = 'tasknote')) AS has_tasknote,
               COUNT(*) OVER () AS group_count
        FROM counts JOIN selected t ON t.id = counts.task_id
        LEFT JOIN current ON current.id = t.id
        ORDER BY counts.events DESC, t.id
        LIMIT ?
        """, (*params, *bounds, ROW_LIMIT),
    ).fetchall()
    agents = db.execute(
        f"""
        WITH selected AS ({selection}), counts AS (
            SELECT e.actor, COUNT(*) AS events, COUNT(CASE WHEN e.action = 'done' THEN 1 END) AS completions
            FROM events e JOIN board_principals p ON p.ledger_key = e.actor AND p.kind = 'agent'
            JOIN selected ON selected.id = e.task_id
            WHERE e.at >= ? AND e.at < ? GROUP BY e.actor
        )
        SELECT counts.actor, COALESCE(NULLIF(p.name, ''), NULLIF(p.username, ''), p.ledger_key) AS name,
               counts.events, counts.completions,
               COUNT(*) OVER () AS group_count, SUM(counts.events) OVER () AS event_count
        FROM counts JOIN board_principals p ON p.ledger_key = counts.actor
        ORDER BY counts.events DESC, name, counts.actor
        LIMIT ?
        """, (*params, *bounds, ROW_LIMIT),
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


def closure_sql(context):
    return f"(action = 'update' AND state = 'done' AND json_type({context}, '$.closure') = 'object')"


def dri_intervals(db, bounds, end, scope, context):
    # Carry the last boundary before the window forward. Progress and logs
    # cannot establish a start, and transfer closes the former owner's interval.
    selection, params = scope
    boundary = f"(action IN ({BOUNDARY_ACTIONS}) OR {closure_sql(context)})"
    rows = db.execute(
        f"""
        WITH selected AS ({selection}), seed AS (
            SELECT MAX(events.id) AS id FROM events JOIN selected ON selected.id = events.task_id
            WHERE {boundary} AND at < ? GROUP BY task_id
        )
        SELECT task_id, action, actor, at, id, state FROM events JOIN seed USING (id)
        UNION ALL
        SELECT task_id, action, actor, at, events.id, state FROM events
        JOIN selected ON selected.id = events.task_id
        WHERE {boundary} AND at >= ? AND at < ?
        ORDER BY task_id, id
        """, (*params, bounds[0], *bounds),
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


def worker_intervals(db, bounds, first, end, scope):
    intervals = []
    coverage = {"measured_runs": 0, "unclosed_runs": 0, "invalid_runs": 0}
    selection, params = scope
    rows = db.execute(
        f"""
        WITH selected AS ({selection}), timings AS (
            SELECT worker, started_at,
                   CASE WHEN status IN ('reported', 'collected') THEN reported_at
                        WHEN status = 'stopped' THEN finalized_at END AS ended_at
            FROM worker_runs JOIN selected ON selected.id = worker_runs.task_id
            WHERE started_at < ?
        )
        SELECT * FROM timings WHERE ended_at IS NULL OR started_at >= ? OR ended_at > ?
        """, (*params, bounds[1], bounds[0], bounds[0]),
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


class Timing:
    def __init__(self):
        self.eligible = 0
        self.durations = []
        self.examples = []

    def observe(self, task, start, finish):
        self.eligible += 1
        since = ledger_seconds(start["at"]) if start is not None else None
        until = ledger_seconds(finish["at"]) if finish is not None else None
        if since is None or until is None or since >= until:
            return
        self.durations.append(until - since)
        if len(self.examples) < 3:
            self.examples.append({
                "task": projects.row_reference(task), "start_event": start["event_id"], "end_event": finish["event_id"],
            })

    def summarize(self):
        measured = len(self.durations)
        return {
            "eligible": self.eligible, "measured": measured, "unknown": self.eligible - measured,
            "median_seconds": median(self.durations) if measured else None, "examples": self.examples,
        }


def ticket_flow(db, scope, context, bounds, first, width, count):
    selection, params = scope
    closed = closure_sql(context)
    rows = db.execute(
        f"""
        WITH selected AS ({selection}), completions AS (
            SELECT task_id, MAX(events.id) AS last_done
            FROM events JOIN selected ON selected.id = events.task_id
            WHERE action = 'done' AND at >= ? AND at < ? GROUP BY task_id
        )
        SELECT selected.id, project_prefix, project_number, events.id AS event_id, at,
               CASE WHEN {closed} THEN 'closed'
                    WHEN action = 'transfer' THEN 'reopen' ELSE action END AS stage
        FROM events JOIN selected ON selected.id = events.task_id
        LEFT JOIN completions ON completions.task_id = events.task_id
        WHERE (at >= ? AND at < ? AND (action = 'done' OR {closed} OR
               (action = 'add' AND events.id = (
                   SELECT MIN(added.id) FROM events AS added
                   WHERE added.task_id = events.task_id AND added.action = 'add'
               ))))
           OR (events.id <= completions.last_done AND (action IN ('start', 'done') OR {closed}
               OR (action = 'transfer' AND json_extract({context}, '$.ownership_action') = 'reopen')))
        ORDER BY selected.id, events.id
        """, (*params, *bounds, *bounds),
    )
    counts = {"new": 0, "done": 0, "closed": 0}
    trend = [counts.copy() for _ in range(count)]
    turnaround = Timing()
    for _, history in groupby(rows, key=lambda row: row["id"]):
        start = None
        for row in history:
            stage = row["stage"]
            if stage == "start" and start is None:
                start = row
            at = ledger_seconds(row["at"])
            if stage in ("add", "done", "closed") and at is not None and 0 <= at - first < width * count:
                key = "new" if stage == "add" else stage
                counts[key] += 1
                trend[(at - first) // width][key] += 1
                if stage == "done":
                    turnaround.observe(row, start, row)
            if stage in ("done", "closed", "reopen"):
                start = None
    return {**counts, "start_to_done": turnaround.summarize()}, trend


def peer_timings(db, scope, context, bounds, first, end):
    counts = dict.fromkeys(("opened", "pending", "awaiting_ack", "acknowledged", "canceled", "closed", "already_started"), 0)
    stages = {name: Timing() for name in ("add_to_start", "start_to_result", "add_to_result", "result_to_ack")}
    if tasks_storage.has_table(db, "peer_asks"):
        selection, params = scope
        rows = db.execute(
            f"""
            WITH selected AS ({selection}), cohort AS (
                SELECT selected.id, selected.project_prefix, selected.project_number,
                       peer_asks.requester, peer_asks.result_event_id, worker_runs.started_at AS linked_start,
                       opened.id AS added_id
                FROM peer_asks JOIN selected ON selected.id = peer_asks.task_id
                LEFT JOIN worker_runs ON worker_runs.id = peer_asks.review_run_id
                JOIN events AS opened ON opened.id = (
                    SELECT MIN(id) FROM events WHERE task_id = peer_asks.task_id AND action = 'add'
                )
                WHERE opened.at >= ? AND opened.at < ?
            )
            SELECT cohort.*, events.id AS event_id, at,
                   CASE WHEN {closure_sql(context)}
                             THEN COALESCE(json_extract({context}, '$.closure.peer_outcome'), 'closed')
                        WHEN action = 'done' AND json_extract({context}, '$.peer_correction') IS NOT NULL THEN 'corrected'
                        WHEN action = 'transfer' AND events.id = cohort.result_event_id THEN 'result'
                        WHEN action = 'done' AND actor = requester THEN 'ack'
                        ELSE action END AS stage
            FROM cohort JOIN events ON events.task_id = cohort.id AND events.id >= cohort.added_id
            WHERE at < ? AND (action IN ('add', 'start', 'transfer', 'done') OR {closure_sql(context)})
            ORDER BY cohort.id, events.id
            """, (*params, *bounds, bounds[1]),
        )
        for _, history in groupby(rows, key=lambda row: row["id"]):
            added = next(history)
            added_at = ledger_seconds(added["at"])
            if added_at is None or not first <= added_at < end:
                continue
            counts["opened"] += 1
            start = result = acknowledged = None
            closed = None
            corrected = False
            for row in history:
                stage = row["stage"]
                if stage == "start" and start is None and result is None:
                    start = row
                elif stage == "result":
                    result = row
                elif stage == "ack" and result is not None:
                    acknowledged = row
                    break
                elif stage in ("canceled", "superseded", "closed_without_response", "closed"):
                    closed = "canceled" if stage == "canceled" else "closed"
                elif stage == "corrected":
                    acknowledged = row
                    closed = None
                    corrected = True
                    break

            if start is not None:
                start_at = ledger_seconds(start["at"])
                prestarted = start_at is not None and start_at < added_at
                if prestarted and start["at"] == added["linked_start"]:
                    counts["already_started"] += 1
                else:
                    if prestarted:
                        start = None
                    stages["add_to_start"].observe(added, added, start)
            elif not closed and not corrected:
                stages["add_to_start"].observe(added, added, None)

            if result is not None:
                stages["start_to_result"].observe(added, start, result)
                stages["add_to_result"].observe(added, added, result)
            if acknowledged is not None and not corrected:
                stages["result_to_ack"].observe(added, result, acknowledged)
            if closed:
                counts[closed] += 1
            elif acknowledged is not None:
                counts["acknowledged"] += 1
            elif result is not None:
                counts["awaiting_ack"] += 1
            else:
                counts["pending"] += 1
    return {**counts, "timings": {name: timing.summarize() for name, timing in stages.items()}}


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


def snapshot(db, period, now=None, *, base="", watcher_task=None, project=None):
    count, width = PERIODS[period]
    # Ledger timestamps have whole-second precision. Keep the upper bound
    # exclusive without dropping events from the current partial second.
    end = ceil((now or datetime.now(timezone.utc)).timestamp())
    first = end - count * width
    start_at, end_at = utc_text(first), utc_text(end)
    # Compare whole UTC seconds consistently for both Z and legacy +00:00 rows.
    bounds = (start_at[:-1], end_at[:-1])
    scope = task_scope(db, project)
    selection, params = scope
    context = "events.context" if has_event_context(db) else "'{}'"
    active = bucket_peaks(dri_intervals(db, bounds, end, scope, context), first, width, count)
    worker_runs, workers = worker_intervals(db, bounds, first, end, scope)
    worker_counts = bucket_peaks(worker_runs, first, width, count)
    flow, trend = ticket_flow(db, scope, context, bounds, first, width, count)
    measurements = None if watcher_task is None else db.execute(
        f"""
        WITH selected AS ({selection})
        SELECT json_extract({context}, '$.watcher_measurements')
        FROM events JOIN selected ON selected.id = events.task_id
        WHERE task_id = ? AND action = 'log' AND json_type({context}, '$.watcher_measurements') = 'object'
        ORDER BY events.id DESC LIMIT 1
        """, (*params, watcher_task),
    ).fetchone()
    return {
        "period": period, "project": project, "from": start_at, "to": end_at, "bucket_seconds": width,
        "ticket_flow": flow, "peer_requests": peer_timings(db, scope, context, bounds, first, end),
        "activity": activity_counts(db, bounds, base, scope), "worker_hours": workers,
        **({"watcher_measurements": json.loads(measurements[0])} if measurements else {}),
        "buckets": [
            {"from": utc_text(first + index * width), "to": utc_text(first + (index + 1) * width),
             "dris": active[index], "workers": worker_counts[index], **trend[index]}
            for index in range(count)
        ],
    }
