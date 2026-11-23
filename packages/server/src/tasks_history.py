"""Reconstruct available record fields at a recorded Activity event."""

import tasks_core as ledger
import tasks_peer_asks as peer_asks
import tasks_projects as projects
import watches


def previous_content(db, task, event_id):
    fields = {"title": task["title"], "brief": task["brief"]}
    if not ledger.has_event_context(db):
        if event_id != task["event_id"]:
            return dict.fromkeys(fields)
        return fields

    changes = db.execute(
        """
        SELECT note, context FROM events
        WHERE task_id = ? AND id > ? AND action = 'log'
          AND (json_extract(context, '$.audit.action') = 'edit-task-details' OR note = 'Updated this goal.')
        ORDER BY id DESC
        """,
        (task["id"], event_id),
    )
    goal = ledger.task_goal_kind(db, task["id"])
    for change in changes:
        audit = ledger.event_context(change).get("audit", {})
        if audit.get("action") == "edit-task-details":
            previous = audit.get("previous", {})
            for field in fields.keys() & previous.keys():
                fields[field] = previous[field]
        elif goal:
            # Older goal edits replaced the title without recording its previous value.
            fields["title"] = None

    if watches.has_table(db, "peer_asks") and peer_asks.find(db, task["id"]) is not None:
        # Generated peer-request titles could change before the first explicit title edit.
        authored_title = db.execute(
            """
            SELECT 1 FROM events WHERE task_id = ? AND id <= ? AND action = 'log'
              AND json_extract(context, '$.audit.action') = 'edit-task-details'
              AND json_type(context, '$.audit.current.title') = 'text'
            LIMIT 1
            """,
            (task["id"], event_id),
        ).fetchone()
        if authored_title is None:
            fields["title"] = None
    return fields


def version(db, task, event_id):
    event = db.execute("SELECT * FROM events WHERE task_id = ? AND id = ?",
                       (task["id"], event_id)).fetchone()
    if event is None:
        return None
    state = db.execute(
        """
        SELECT * FROM events WHERE task_id = ? AND id <= ? AND action NOT IN ('source-event', 'log')
        ORDER BY id DESC LIMIT 1
        """,
        (task["id"], event_id),
    ).fetchone()
    visible = watches.display_event(event)
    result = {
        "key": projects.row_reference(task),
        **previous_content(db, task, event_id),
        "event": {
            "id": event_id,
            "at": visible["at"],
            "actor": visible["actor"],
            "label": ledger.event_label(visible["action"], ledger.event_context(visible)),
        },
    }
    if state is not None:
        result.update(
            state="closed" if state["state"] == "done" and isinstance(
                ledger.event_context(state).get("closure"), dict,
            ) else state["state"],
            note=state["note"],
        )
    return result
