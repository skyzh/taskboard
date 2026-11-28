"""Count confirmed app wakes until the signed recipient records new work."""

import tasks_audit as audit


LIMIT = 3
PAUSED_REASON = "three_accepted_without_signed_activity"
_ACCEPTED = "accept-app-wake"
_SIGNED = "recover-signed-work"
_OPERATOR = "resume-app-wake"


def status(db, owner, project):
    record_type = f"app-wake:{project}"
    recovery = db.execute(
        """SELECT id, operation, at FROM audit_events
           WHERE actor = ? AND record_type = ? AND operation IN (?, ?)
           ORDER BY id DESC LIMIT 1""", (owner, record_type, _SIGNED, _OPERATOR),
    ).fetchone()
    accepted = db.execute(
        """SELECT at FROM audit_events
           WHERE actor = ? AND record_type = ? AND operation = ? AND id > ?
           ORDER BY id""", (owner, record_type, _ACCEPTED, recovery["id"] if recovery else 0),
    ).fetchall()
    paused = len(accepted) >= LIMIT
    return {
        "paused": paused,
        "reason": PAUSED_REASON if paused else None,
        "accepted_count": len(accepted),
        "paused_at": accepted[LIMIT - 1]["at"] if paused else None,
        "latest_recovery_at": recovery["at"] if recovery else None,
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
    if status(db, owner, project)["accepted_count"]:
        audit.record(db, scope="private", actor=owner, operation=_SIGNED,
                     record_type=f"app-wake:{project}", record_id=str(event_id), at=at)


def operator_resume(db, owner, project, at):
    if status(db, owner, project)["paused"]:
        audit.record(db, scope="operator", actor=owner, operation=_OPERATOR,
                     record_type=f"app-wake:{project}", record_id="resume", at=at)
        return True
    return False
