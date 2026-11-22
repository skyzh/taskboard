"""Correlate opt-in Ready watcher observations with board event metadata."""

from collections import defaultdict
from datetime import datetime
import json
from math import isfinite


ADVANCES = {"start", "resume", "update"}
ACTIVITY_SCHEMA = "taskboard-watcher-measurements/v1"
COHORTS = {"bounded": "Bounded watcher baseline", "count-only": "Ready-count watcher pilot"}
METRICS = (
    "ready_at_first_idle", "idle_to_wake_seconds", "wake_to_start_seconds",
    "working_seconds", "advanced_tasks", "completed_tasks",
)


def activity_measurements(details):
    """Normalize an owner's reviewed cohort summary for the Stats read model."""
    if not details or not details.startswith("{"):
        return None
    try:
        payload = json.loads(details)
    except (ValueError, RecursionError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != ACTIVITY_SCHEMA:
        return None
    cohorts = payload.get("cohorts")
    if payload.keys() != {"schema", "cohorts"} or not isinstance(cohorts, list) or not 1 <= len(cohorts) <= len(COHORTS):
        raise ValueError("Provide one or two watcher cohorts.")
    normalized = {}
    for cohort in cohorts:
        if not isinstance(cohort, dict) or cohort.keys() != {"key", "run_count", "metrics"}:
            raise ValueError("Provide a cohort key, run count, and metrics.")
        key, run_count, metrics = cohort["key"], cohort["run_count"], cohort["metrics"]
        if not isinstance(key, str) or key not in COHORTS or key in normalized:
            raise ValueError("Use a distinct bounded or count-only cohort key.")
        if type(run_count) is not int or run_count < 1:
            raise ValueError("A cohort must contain at least one run.")
        if not isinstance(metrics, dict) or metrics.keys() != set(METRICS):
            raise ValueError("Provide all watcher metrics, including unmeasured ones.")
        for value in metrics.values():
            if not isinstance(value, dict) or value.keys() != {"mean", "measured_runs"}:
                raise ValueError("Provide a mean and measured run count for each metric.")
            count, mean = value["measured_runs"], value["mean"]
            if type(count) is not int or not 0 <= count <= run_count:
                raise ValueError("Measured runs must fit within the cohort run count.")
            if count == 0 and mean is None:
                continue
            if count > 0 and (type(mean) is int or type(mean) is float and isfinite(mean)) and mean >= 0:
                continue
            raise ValueError("Use a nonnegative mean for measured runs and null for missing measurements.")
        normalized[key] = {**cohort, "label": COHORTS[key]}
    return {"cohorts": [normalized[key] for key in COHORTS if key in normalized]}


def instant(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Measurement timestamps must include a UTC offset.")
    return result


def second(value):
    return instant(value).replace(microsecond=0)


def start_wake(at, wakes):
    start = second(at)
    fired = [wake["fired_at"] for wake in wakes if wake["fired_at"] is not None]
    earlier = [value for value in map(second, fired) if value < start]
    if earlier:
        return "after-fired-wake", int((start - max(earlier)).total_seconds())
    if any(second(value) == start for value in fired) or any(second(wake["at"]) == start for wake in wakes):
        return "same-second-unordered", None
    if any(second(wake["at"]) < start for wake in wakes):
        return "after-wake-request", None
    return "without-observed-wake", None


def episode(owner, idle, working, end, end_status, requests, events, fired):
    wakes = [
        {"at": request["at"], "ready_count": request.get("ready_count"), "reminder": request.get("reminder"),
         "fired_at": fired.get(request["reminder"]) if request.get("reminder") else None}
        for request in requests
    ]
    complete = end_status == "idle"
    duration = round((instant(end) - instant(working)).total_seconds(), 3) if complete and working else None
    first = idle["at"] if idle else None
    work = []
    if first is not None:
        lower, upper = second(first), second(end)
        # Ledger timestamps resolve to seconds. Equal seconds across sources do
        # not establish which side of an observed boundary the event belongs on.
        work = [event for event in events if lower < second(event["at"]) < upper]
    start = min((event for event in work if event["action"] == "start"),
                key=lambda event: (instant(event["at"]), event["id"]), default=None)
    wake_status, latency = start_wake(start["at"], wakes) if start else (None, None)
    return {
        "owner": owner, "sampled_idle_at": first, "ready_count": idle.get("ready_count") if idle else None,
        "ready_observed_at": idle.get("ready_observed_at") if idle else None,
        "working_sample_at": working, "sampled_work_seconds": duration,
        "end_at": end, "end_status": end_status, "complete": complete, "wakes": wakes,
        "next_start": {key: start[key] for key in ("id", "task_id", "at")} if start else None,
        "start_wake": wake_status, "wake_to_start_seconds": latency,
        "checked_tasks": len({event["task_id"] for event in work if event["action"] == "log"
                              and (event.get("context") or {}).get("audit", {}).get("action") == "checklist-complete"})
        if first is not None else None,
        "advanced_tasks": len({event["task_id"] for event in work if event["action"] in ADVANCES
                               and "closure" not in (event.get("context") or {})})
        if first is not None else None,
        "completed_tasks": len({event["task_id"] for event in work if event["action"] == "done"})
        if first is not None else None,
    }


def summarize(observations, events, fired):
    """Return only sampled episode metadata from an isolated pilot cohort.

    `fired` maps exact reminder references to independently verified times.
    An explicit process end bounds a censored episode. Otherwise incomplete
    episodes cover only the observed portion. The work duration uses sampled
    timestamps. A start can follow a fired reminder without proving cause.
    """
    owners, history = defaultdict(list), defaultdict(list)
    for row in observations:
        owners[row["owner"]].append(row)
    for event in events:
        if event["actor"] in owners:
            history[event["actor"]].append(event)

    episodes = []
    for owner, rows in owners.items():
        idle, working, requests = None, None, []
        previous_status, last_observed_at = None, None
        for row in sorted(rows, key=lambda row: instant(row["at"])):
            if row["kind"] == "wake-requested":
                if idle is not None:
                    requests.append(row)
                    last_observed_at = row["at"]
                continue
            status = row["status"]
            if status == "idle":
                if idle is not None and working is not None:
                    episodes.append(episode(owner, idle, working, row["at"], status, requests, history[owner], fired))
                    idle, working, requests = None, None, []
                if idle is None:
                    idle = row
                    working = None
            elif status == "working":
                if working is None:
                    working = row["at"]
            elif row["kind"] == "end" or (status in {"stopped", "unavailable"} and status != previous_status):
                episodes.append(episode(owner, idle, working, row["at"], status, requests, history[owner], fired))
                idle, working, requests = None, None, []
            previous_status, last_observed_at = status, row["at"]
        if previous_status in {"idle", "working"}:
            episodes.append(episode(owner, idle, working, last_observed_at, "observation-ended",
                                    requests, history[owner], fired))
    return episodes
