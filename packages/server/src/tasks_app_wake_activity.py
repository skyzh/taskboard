"""Count confirmed app wakes until the signed recipient records new work."""

from datetime import datetime, timezone

import tasks_audit as audit
from tasks_wake_subscriptions import MANAGED_READY_PROJECTS


LIMIT = 3
PAUSED_REASON = "three_accepted_without_signed_activity"
_ACCEPTED = "accept-app-wake"
_SIGNED = "recover-signed-work"
_OPERATOR = "resume-app-wake"


def observation_at():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def whole_second(value):
    return datetime.fromisoformat(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def after_recovery(observed, observed_at, recovery):
    if recovery is None:
        return True
    boundary = datetime.fromisoformat(recovery)
    # An older sender or board may have recorded only whole seconds. A late
    # audit cannot determine which side of the recovery that confirmation fell on.
    if "." not in observed_at or "." not in recovery:
        observed = observed.replace(microsecond=0)
        boundary = boundary.replace(microsecond=0)
    return observed > boundary


def status(db, owner, project):
    record_type = f"app-wake:{project}"
    recovery = db.execute(
        """SELECT id, operation, at FROM audit_events
           WHERE actor = ? AND record_type = ? AND operation IN (?, ?)
           ORDER BY id DESC LIMIT 1""", (owner, record_type, _SIGNED, _OPERATOR),
    ).fetchone()
    observations = db.execute(
        """SELECT id, at FROM audit_events
           WHERE actor = ? AND record_type = ? AND operation = ?""", (owner, record_type, _ACCEPTED),
    )
    accepted = []
    for row in observations:
        observed = datetime.fromisoformat(row["at"])
        if after_recovery(observed, row["at"], recovery["at"] if recovery else None):
            accepted.append((observed, row["id"], row["at"]))
    accepted.sort()
    paused = len(accepted) >= LIMIT
    return {
        "paused": paused,
        "reason": PAUSED_REASON if paused else None,
        "accepted_count": len(accepted),
        "paused_at": whole_second(accepted[LIMIT - 1][2]) if paused else None,
        "latest_recovery_at": whole_second(recovery["at"]) if recovery else None,
        "recovery_kind": ({_SIGNED: "signed_work", _OPERATOR: "operator_resume"}[recovery["operation"]]
                          if recovery else None),
    }


def record_acceptance(db, owner, project, event_key, at):
    """Record one confirmed carrier envelope in the caller's write transaction."""
    record_type = f"app-wake:{project}"
    existing = db.execute(
        """SELECT 1 FROM audit_events
           WHERE actor = ? AND record_type = ? AND operation = ? AND record_id = ?""",
        (owner, record_type, _ACCEPTED, event_key),
    ).fetchone()
    if existing is None:
        audit.record(db, scope="operator", actor=owner, operation=_ACCEPTED,
                     record_type=record_type, record_id=event_key, at=at)


def signed_recovery(db, owner, project, event_id, at):
    if project in MANAGED_READY_PROJECTS:
        audit.record(db, scope="private", actor=owner, operation=_SIGNED,
                     record_type=f"app-wake:{project}", record_id=str(event_id), at=at)


def operator_resume(db, owner, project, at):
    if status(db, owner, project)["paused"]:
        audit.record(db, scope="operator", actor=owner, operation=_OPERATOR,
                     record_type=f"app-wake:{project}", record_id="resume", at=at)
        return True
    return False
