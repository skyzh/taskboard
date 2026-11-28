"""Board-wide agent delivery choices and checked-worker readback."""

from datetime import datetime, timedelta

import tasks_app_wake_activity as app_wake_activity
import tasks_ring1 as ring1
import tasks_runtime_observations as runtime_observations

MECHANISMS = frozenset({"raft_app", "off"})
CUE_KINDS = (
    "ready", "review", "report", "readiness", "request", "decision", "reminder", "in_progress",
    "shared_ready", "worker_checkpoint", "resolved-wait", "wait-review", "ask_recheck",
    "goal_review", "goal_explore", "goal_timed", "goal_deadline",
)
RUNTIME_FRESHNESS = timedelta(minutes=3)
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS agent_delivery_enrollments (
        owner TEXT PRIMARY KEY REFERENCES board_principals (ledger_key),
        mechanism TEXT NOT NULL CHECK (mechanism IN ('raft_app', 'off')),
        revision BIGINT NOT NULL CHECK (revision > 0)
    )""",
    """CREATE TABLE IF NOT EXISTS agent_delivery_runtime (
        owner TEXT PRIMARY KEY REFERENCES agent_delivery_enrollments (owner),
        enrollment_revision BIGINT NOT NULL,
        revision TEXT NOT NULL,
        mode TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('running', 'stopped')),
        observed_at TEXT NOT NULL
    )""",
)


def directory(db, principal, default_project, roster):
    """Use visible participated projects, applying the agent's Ring 1 grant as well."""
    visible, args = ring1.visibility(db, "prefix", principal)
    projects = db.execute(f"SELECT prefix FROM projects WHERE {visible} ORDER BY prefix", args).fetchall()
    agents = {}
    for row in projects:
        project = row["prefix"]
        for agent in runtime_observations.project_agents(
            db, project, roster if project == default_project else {}, principal=principal,
        ):
            owner = agent["ledger_key"]
            entry = agents.setdefault(owner, {
                "agent_id": owner, "name": agent["name"], "project_scopes": [],
            })
            entry["project_scopes"].append(project)
    return agents


def get(db, owner):
    row = db.execute(
        "SELECT mechanism, revision FROM agent_delivery_enrollments WHERE owner = ?", (owner,),
    ).fetchone()
    return dict(row) if row is not None else {"mechanism": "off", "revision": 0}


def save(db, owner, mechanism, revision):
    """Atomically compare and advance the revision. Return None for a stale editor."""
    prior = get(db, owner)
    if prior["revision"] != revision:
        return None
    if revision and prior["mechanism"] == mechanism:
        return False
    if revision == 0:
        updated = db.execute(
            """INSERT INTO agent_delivery_enrollments (owner, mechanism, revision) VALUES (?, ?, 1)
               ON CONFLICT (owner) DO NOTHING""", (owner, mechanism),
        )
    else:
        updated = db.execute(
            """UPDATE agent_delivery_enrollments SET mechanism = ?, revision = revision + 1
               WHERE owner = ? AND revision = ?""", (mechanism, owner, revision),
        )
    return True if updated.rowcount else None


def report(db, owner, enrollment_revision, *, revision, state, observed_at):
    """Record only a readback for the enrollment the running worker checked."""
    db.execute(
        """INSERT INTO agent_delivery_runtime (owner, enrollment_revision, revision, mode, state, observed_at)
           SELECT owner, revision, ?, 'raft_app', ?, ? FROM agent_delivery_enrollments
           WHERE owner = ? AND revision = ?
           ON CONFLICT (owner) DO UPDATE SET enrollment_revision = excluded.enrollment_revision,
               revision = excluded.revision, mode = excluded.mode, state = excluded.state,
               observed_at = excluded.observed_at
           WHERE excluded.observed_at >= agent_delivery_runtime.observed_at""",
        (revision, state, observed_at, owner, enrollment_revision),
    )


def status(db, entry, *, can_edit, now):
    owner = entry["agent_id"]
    enrollment = get(db, owner)
    observed = db.execute(
        """SELECT revision, mode, state, observed_at FROM agent_delivery_runtime
           WHERE owner = ? AND enrollment_revision = ?""", (owner, enrollment["revision"]),
    ).fetchone()
    runtime = {"state": "unknown", "revision": None, "mode": None, "observed_at": None}
    if observed:
        at = datetime.fromisoformat(observed["observed_at"])
        if now - RUNTIME_FRESHNESS <= at <= now:
            runtime = dict(observed)
        else:
            runtime["observed_at"] = observed["observed_at"]
    if enrollment["mechanism"] == runtime["mode"] == "raft_app" and runtime["state"] == "running":
        if app_wake_activity.status(db, owner, None)["paused"]:
            cue = {"state": "disabled", "reason": "Delivery paused after three wakes without signed activity."}
        else:
            cue = {"state": "enabled"}
    elif runtime["state"] == "stopped":
        cue = {"state": "disabled", "reason": "The worker verified that delivery is stopped."}
    else:
        cue = {"state": "unknown", "reason": "No current worker readback for this enrollment."}
    return {
        **entry, **enrollment, "can_edit": can_edit, "runtime": runtime,
        "cues": [{"kind": kind, **cue} for kind in CUE_KINDS],
    }
