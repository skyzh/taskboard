"""Read an opt-in daily agent usage aggregate and estimate recorded worker time."""

from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import re
from uuid import UUID
from zoneinfo import ZoneInfo


TIME_ZONE = "America/Los_Angeles"
MAX_BYTES = 128 * 1024
TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
COVERAGE = {"tokens": "local_codex", "worker_time": "recorded_taskboard_workers"}
AMOUNT = re.compile(r"[0-9]{1,12}(?:\.[0-9]{1,8})?")
CURRENCY = re.compile(r"[A-Z]{3}")


def configuration(environment):
    value = environment.get("TASKS_AGENT_USAGE_SNAPSHOT")
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("Use an absolute path for TASKS_AGENT_USAGE_SNAPSHOT.")
    return path


def token_counts(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != set(TOKEN_FIELDS)
            or any(type(count) is not int or count < 0 for count in value.values())
            or value["cached_input_tokens"] > value["input_tokens"]
            or value["reasoning_output_tokens"] > value["output_tokens"]):
        raise ValueError("Invalid aggregate token counts.")
    return {**value, "total_tokens": value["input_tokens"] + value["output_tokens"]}


def cost_estimate(value):
    if value is None:
        return None
    if not isinstance(value, dict) or value.keys() != {"amount", "currency", "basis"}:
        raise ValueError("Use an amount, currency, and basis for an estimate.")
    amount, currency, basis = value["amount"], value["currency"], value["basis"]
    if (not isinstance(amount, str) or not AMOUNT.fullmatch(amount)
            or not isinstance(currency, str) or not CURRENCY.fullmatch(currency)
            or not isinstance(basis, str) or not 0 < len(basis.strip()) <= 160 or not basis.isprintable()):
        raise ValueError("Invalid cost estimate.")
    return {"amount": amount, "currency": currency, "basis": basis.strip()}


def load(path, server_id):
    if path is None:
        return None
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return None
        report = json.loads(raw)
        if (not isinstance(report, dict) or report.keys() != {"version", "server_id", "day", "agents"}
                or type(report["version"]) is not int or report["version"] != 1
                or report["server_id"] != server_id or not isinstance(report["agents"], list)):
            return None
        day = date.fromisoformat(report["day"])
        if day.isoformat() != report["day"]:
            return None
        zone = ZoneInfo(TIME_ZONE)
        start = datetime.combine(day, time.min, zone).astimezone(timezone.utc)
        end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
        agents = {}
        for row in report["agents"]:
            if (not isinstance(row, dict) or set(row) - {"subject", "tokens", "estimated_cost"}
                    or not isinstance(row.get("subject"), str)):
                return None
            subject = str(UUID(row["subject"]))
            if subject in agents:
                return None
            counts = token_counts(row.get("tokens"))
            estimate = cost_estimate(row.get("estimated_cost"))
            if estimate is not None and counts is None:
                return None
            agents[subject] = {"tokens": counts, "estimated_cost": estimate}
        return start, end, agents
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def utc_text(instant):
    return instant.isoformat(timespec="seconds").replace("+00:00", "Z")


def worker_times(db, owners, start, end):
    summaries = {
        owner: {"elapsed_seconds": None, "counted_runs": 0, "incomplete_runs": 0}
        for owner in owners
    }
    if not summaries:
        return summaries
    rows = db.execute(
        """
        SELECT assignment.actor, run.started_at,
               COALESCE(run.reported_at, CASE WHEN run.status = 'stopped' THEN run.finalized_at END) AS ended_at
        FROM worker_runs AS run
        JOIN events AS assignment ON assignment.id = (
            SELECT MIN(event.id) FROM events AS event
            WHERE event.task_id = run.task_id AND event.action = 'log'
              AND json_extract(event.context, '$.audit.action') = 'worker-delegate'
              AND CAST(json_extract(event.context, '$.audit.run_id') AS BIGINT) = run.id
        )
        WHERE run.started_at < ?
          AND COALESCE(run.reported_at, CASE WHEN run.status = 'stopped' THEN run.finalized_at END, ?) > ?
        """, (utc_text(end), utc_text(end), utc_text(start)),
    )
    for row in rows:
        summary = summaries.get(row["actor"])
        if summary is None:
            continue
        try:
            start_at = datetime.fromisoformat(row["started_at"])
            end_at = datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None
            if start_at.tzinfo is None or end_at is None or end_at.tzinfo is None or end_at < start_at:
                raise ValueError("A recorded worker interval requires start and end times.")
        except ValueError:
            summary["incomplete_runs"] += 1
            continue
        elapsed = int((min(end, end_at) - max(start, start_at)).total_seconds())
        summary["elapsed_seconds"] = (summary["elapsed_seconds"] or 0) + elapsed
        summary["counted_runs"] += 1
    return summaries


def report(db, server_id, snapshot):
    result = {"scope": "board", "window": None, "coverage": COVERAGE, "items": []}
    if snapshot is None:
        return result
    start, end, agents = snapshot
    result["window"] = {"start": utc_text(start), "end": utc_text(end), "time_zone": TIME_ZONE}
    owners = {}
    for row in db.execute(
        "SELECT ledger_key, subject, name FROM board_principals WHERE server_id = ? AND kind = 'agent'",
        (server_id,),
    ):
        if row["subject"] in agents:
            owners[row["ledger_key"]] = {"name": row["name"] or row["ledger_key"], **agents[row["subject"]]}
    timings = worker_times(db, owners, start, end)
    result["items"] = [
        {"owner": key, **owners[key], "worker_time": timings[key]} for key in sorted(owners)
    ]
    return result
