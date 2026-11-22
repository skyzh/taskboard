"""Taskboard's local merge queue, backed by authenticated Activity records."""

import json

from taskboard_client.merge_details import STATUS_AFTER_ACTION, merge_details

import tasks_projects as projects


SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS merge_candidates (
        submission_event_id INTEGER PRIMARY KEY REFERENCES events (id),
        task_id INTEGER NOT NULL REFERENCES tasks (id),
        candidate TEXT NOT NULL,
        target TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'submitted',
        review_event_id INTEGER REFERENCES events (id),
        status_event_id INTEGER NOT NULL REFERENCES events (id),
        squash TEXT,
        UNIQUE (task_id, candidate, target)
    )
    """,
    "CREATE INDEX IF NOT EXISTS merge_candidates_latest ON merge_candidates (task_id, submission_event_id DESC)",
)


# Aliases and retries of a landed squash share the first accepted receipt.
ACCEPTED_SQUASHES = """
    accepted_squashes AS (
        SELECT task_id, squash, MIN(status_event_id) AS merged_event
        FROM merge_candidates
        WHERE status = 'merged' AND squash IS NOT NULL
        GROUP BY task_id, squash
    )
"""


def apply_activity(db, task, actor, event_id, details):
    """Apply a typed event in the Activity write transaction."""
    payload = merge_details(details)
    if payload is None:
        return
    if not projects.merge_queue_enabled(projects.row_project(task)):
        raise ValueError("The local merge queue is available for Taskboard tickets only.")

    action, candidate, target = (payload[key] for key in ("action", "candidate", "target"))
    number, owner = task["id"], task["dri"]
    if action != "review" and actor != owner:
        raise ValueError("The task owner records local merge queue changes.")
    if action == "submit":
        if task["display_state"] == "closed":
            raise ValueError("Reopen the ticket before submitting a merge candidate.")
        db.execute(
            """
            INSERT INTO merge_candidates (submission_event_id, task_id, candidate, target, status_event_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (task_id, candidate, target) DO NOTHING
            """,
            (event_id, number, candidate, target, event_id),
        )
        return

    current = db.execute(
        """
        SELECT merge_candidates.*, events.actor AS submitter
        FROM merge_candidates JOIN events ON events.id = submission_event_id
        WHERE merge_candidates.task_id = ? ORDER BY submission_event_id DESC LIMIT 1
        """,
        (number,),
    ).fetchone()
    if current is None or (current["candidate"], current["target"]) != (candidate, target):
        raise ValueError("Record the exact current submitted candidate and target.")
    submission = current["submission_event_id"]
    if action == "review":
        if actor in (owner, current["submitter"]):
            raise ValueError("An independent reviewer must record the merge review.")
        if current["review_event_id"] is None:
            db.execute(
                """
                UPDATE merge_candidates SET status = 'reviewed', review_event_id = ?, status_event_id = ?
                WHERE submission_event_id = ?
                """,
                (event_id, event_id, submission),
            )
        return

    if current["review_event_id"] is None:
        raise ValueError("Record an independent review before updating this merge entry.")
    squash = payload.get("squash")
    if current["status"] == "merged":
        if action == "merged" and squash == current["squash"]:
            return
        raise ValueError("This exact candidate already has a recorded squash.")
    db.execute(
        "UPDATE merge_candidates SET status = ?, status_event_id = ?, squash = ? WHERE submission_event_id = ?",
        (STATUS_AFTER_ACTION[action], event_id, squash, submission),
    )


def recorded_merges(db, task_id, event_ids):
    if not event_ids:
        return {}
    marks = ", ".join("?" for _ in event_ids)
    return dict(db.execute(
        f"""
        SELECT status_event_id, squash FROM merge_candidates
        WHERE task_id = ? AND status = 'merged' AND status_event_id IN ({marks})
        """,
        (task_id, *event_ids),
    ))


def queue(db, *, current_tasks, project, before, limit):
    rows = db.execute(
        f"""
        WITH current AS ({current_tasks})
        SELECT tasks.id, tasks.title, tasks.dri, tasks.project_prefix, tasks.project_number,
               entry.target, entry.candidate, entry.status, entry.review_event_id, events.note
        FROM tasks JOIN merge_candidates AS entry ON entry.task_id = tasks.id
        JOIN current ON current.id = tasks.id
        JOIN events ON events.id = entry.status_event_id
        WHERE tasks.project_prefix = ? AND (? IS NULL OR tasks.id < ?)
          AND current.display_state <> 'closed'
          AND entry.status IN ('reviewed', 'conflict', 'checks_failed')
          AND entry.submission_event_id = (
              SELECT MAX(submission_event_id) FROM merge_candidates WHERE task_id = tasks.id
          )
        ORDER BY tasks.id DESC LIMIT ?
        """,
        (project, before, before, limit + 1),
    ).fetchall()
    visible = rows[:limit]
    return {
        "items": [
            {
                "id": row["id"],
                "task": {"id": row["id"], "key": projects.row_reference(row), "title": row["title"]},
                "owner": row["dri"],
                **{field: row[field] for field in ("target", "candidate", "status", "note", "review_event_id")},
            }
            for row in visible
        ],
        "next_cursor": visible[-1]["id"] if len(rows) > limit else None,
    }
