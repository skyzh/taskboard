"""Prospective runtime evidence for explicitly enrolled agents in a project."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
from uuid import UUID

import tasks_notification_log as notices
import tasks_projects as projects
import tasks_ring1 as ring1
import tasks_storage


RETENTION = timedelta(days=7)
MAX_GAP = timedelta(minutes=2)
STATES = frozenset(("working", "idle", "unavailable", "unknown"))

SCHEMA = """CREATE TABLE IF NOT EXISTS runtime_observations (
    project TEXT NOT NULL REFERENCES projects (prefix),
    owner TEXT NOT NULL REFERENCES board_principals (ledger_key),
    slot TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    run_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('working', 'idle', 'unavailable', 'unknown')),
    PRIMARY KEY (project, owner, slot)
)"""


def configuration(environ):
    project = environ.get("TASKS_RUNTIME_PILOT_PROJECT")
    subject = environ.get("TASKS_RUNTIME_PILOT_AGENT_SUBJECT")
    configured = defaultdict(set)
    if project or subject:
        if not project or not subject:
            raise ValueError("Configure both a runtime pilot project and a consenting agent subject.")
        configured[projects.prefix(project)].add(str(UUID(subject)))

    if raw := environ.get("TASKS_RUNTIME_ENROLLMENTS"):
        try:
            rows = json.loads(raw)
            if not isinstance(rows, list):
                raise ValueError
            for row in rows:
                if (not isinstance(row, dict) or row.keys() != {"project", "agent_subject"}
                        or not all(isinstance(value, str) for value in row.values())):
                    raise ValueError
                configured[projects.prefix(row["project"])].add(str(UUID(row["agent_subject"])))
        except ValueError:
            raise ValueError(
                "Set TASKS_RUNTIME_ENROLLMENTS to a JSON array of project and agent_subject UUID pairs."
            ) from None
    return {project: frozenset(subjects) for project, subjects in configured.items()}


def utc(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("Supply a UTC timestamp.")
    return parsed.astimezone(timezone.utc)


def timestamp(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def observation(state, at, run_id, now):
    sample = utc(at)
    if (state not in STATES or sample.microsecond
            or not now - MAX_GAP <= sample <= now):
        raise ValueError("Supply a valid state and a recent UTC timestamp with whole seconds.")
    return timestamp(sample), str(UUID(run_id))


def prune_expired(db, now):
    if tasks_storage.has_table(db, "runtime_observations"):
        db.execute("DELETE FROM runtime_observations WHERE observed_at < ?", (timestamp(now - RETENTION),))


def record(db, project, owner, at, run_id, state):
    slot = timestamp(utc(at).replace(second=0))
    db.execute("""INSERT INTO runtime_observations (project, owner, slot, observed_at, run_id, state)
                  VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (project, owner, slot) DO NOTHING""",
               (project, owner, slot, at, run_id, state))
    saved = db.execute("""SELECT observed_at, run_id, state FROM runtime_observations
                          WHERE project = ? AND owner = ? AND slot = ?""",
                       (project, owner, slot)).fetchone()
    return (saved["observed_at"], saved["run_id"], saved["state"]) == (at, run_id, state)


def intervals(samples, start, end, retained_since):
    result = []

    def append(left, right, state):
        if left >= right:
            return
        if result and result[-1]["state"] == state:
            result[-1]["end_at"] = timestamp(right)
        else:
            result.append({"start_at": timestamp(left), "end_at": timestamp(right), "state": state})

    cursor = start
    for first, second in zip(samples, samples[1:]):
        left, right = utc(first["observed_at"]), utc(second["observed_at"])
        if (first["run_id"] != second["run_id"] or first["state"] != second["state"]
                or first["state"] == "unknown" or right - left > MAX_GAP
                or right.replace(second=0) - left.replace(second=0) != timedelta(minutes=1)):
            continue
        low, high = max(start, left, retained_since), min(end, right)
        if high <= low:
            continue
        append(cursor, low, "unknown")
        append(low, high, first["state"])
        cursor = high
    append(cursor, end, "unknown")
    return result


def project_agents(db, project, roster, *, principal):
    agents = notices.project_agents(db, project, roster, principal=principal)
    if ring1.private(db, project):
        members = ring1.members(db, project)
        return [agent for agent in agents if agent["ledger_key"] in members]
    return agents


def listing(db, project, roster, *, principal, subjects, start, end, now):
    agents = project_agents(db, project, roster, principal=principal)
    owners = [agent["ledger_key"] for agent in agents if agent["subject"] in subjects]
    cutoff = now - RETENTION
    samples = defaultdict(list)
    observed_through = None
    if owners:
        placeholders = ", ".join("?" for _ in owners)
        rows = db.execute(f"""SELECT owner, observed_at, run_id, state FROM runtime_observations
                              WHERE project = ? AND owner IN ({placeholders}) AND observed_at >= ? AND observed_at <= ?
                              ORDER BY observed_at""",
                          (project, *owners, timestamp(max(cutoff, start - MAX_GAP)),
                           timestamp(min(now, end + MAX_GAP))))
        for row in rows:
            samples[row["owner"]].append(row)
            observed_through = row["observed_at"]
    return {
        "project": project, "from": timestamp(start), "to": timestamp(end),
        "observed_through": observed_through,
        "agents": [
            {"id": agent["ledger_key"], "name": agent["name"],
             "intervals": intervals(samples[agent["ledger_key"]], start, end, cutoff)}
            for agent in agents
        ],
    }
