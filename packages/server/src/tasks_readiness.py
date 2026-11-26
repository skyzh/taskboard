"""Project work that needs an owner review after a recorded dependency change."""

import tasks_merges as merges
import tasks_projects as projects


def register(db, task, dependency, criteria):
    if dependency is None and criteria is None:
        return None
    if dependency is None or not isinstance(criteria, str) or not 0 < len(criteria.strip()) <= 4000:
        raise ValueError("Provide a dependency ticket and readiness criteria up to 4000 characters.")
    source_id = projects.resolve(db, dependency)
    if source_id == task["id"]:
        raise ValueError("Choose a separate dependency ticket.")
    source = db.execute("SELECT * FROM tasks WHERE id = ?", (source_id,)).fetchone()
    if source is None:
        raise ValueError("The dependency ticket does not exist.")
    after = db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
    return {
        "source_id": source_id, "source_key": projects.row_reference(source),
        "criteria": criteria.strip(), "after_event": after,
    }


def registered(db, task, review):
    if not isinstance(review, dict):
        return False
    source_id = review.get("source_id")
    criteria = review.get("criteria")
    after = review.get("after_event")
    if (type(source_id) is not int or source_id == task["id"] or not isinstance(criteria, str)
            or not criteria.strip() or type(after) is not int or after < 0):
        return False
    source = db.execute("SELECT * FROM tasks WHERE id = ?", (source_id,)).fetchone()
    return source is not None and projects.row_reference(source) == review.get("source_key")


# The board shows one pending item per parked ticket. A newer accepted event
# replaces its evidence without generating a second item or changing its state.
CANDIDATES = f"""
    {merges.ACCEPTED_SQUASHES},
    readiness_pending AS (
        SELECT task.id, task.title, task.project_prefix, task.project_number,
               json_extract(task.context, '$.readiness.criteria') AS criteria,
               source.id AS source_id, source.title AS source_title,
               source.project_prefix AS source_prefix, source.project_number AS source_number,
               evidence.id AS event_id, evidence.at,
               CASE evidence.action WHEN 'done' THEN 'completed'
                    WHEN 'park' THEN 'deferred' ELSE 'merged' END AS event_kind
        FROM current_tasks AS task
        JOIN current_tasks AS source ON source.id = json_extract(task.context, '$.readiness.source_id')
        JOIN events AS evidence ON evidence.id = (
            SELECT MAX(change.id)
            FROM events AS change
            WHERE change.task_id = source.id
              AND change.id > json_extract(task.context, '$.readiness.after_event')
              AND (
                  change.action = 'done'
                  OR (change.action = 'park' AND json_extract(change.context, '$.feature_deferred') = 1)
                  OR EXISTS (
                      SELECT 1 FROM accepted_squashes AS squash
                      WHERE squash.task_id = source.id AND squash.merged_event = change.id
                  )
              )
        )
        WHERE task.state = 'parked' AND task.dri = ?
          AND (? IS NULL OR task.project_prefix = ?)
    )
"""


def query(current_tasks):
    return f"WITH current_tasks AS ({current_tasks}), {CANDIDATES} SELECT * FROM readiness_pending"


def item(row):
    source_number = row["source_number"] or row["source_id"]
    return {
        "id": row["id"], "key": projects.row_reference(row), "title": row["title"],
        "criteria": row["criteria"], "source_id": row["source_id"],
        "source_key": f"{row['source_prefix']}-{source_number}", "source_title": row["source_title"],
        "event_id": row["event_id"], "event_kind": row["event_kind"],
    }


def page(db, current_tasks, owner, project, before, limit, *, bindings=()):
    rows = db.execute(
        query(current_tasks) + " WHERE (? IS NULL OR id < ?) ORDER BY id DESC LIMIT ?",
        (*bindings, owner, project, project, before, before, limit + 1),
    ).fetchall()
    visible = rows[:limit]
    return {
        "items": [item(row) for row in visible],
        "next_cursor": visible[-1]["id"] if len(rows) > limit else None,
    }
