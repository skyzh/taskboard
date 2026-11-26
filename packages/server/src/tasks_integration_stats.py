"""Project-scoped counts from existing GitHub and Raft receipt records."""

import tasks_extensions as extensions
import tasks_github as github
import tasks_projects as projects
import tasks_ring1 as ring1
import tasks_storage
from tasks_core import has_event_context


def _github_identity(row):
    if row["repository"] and row["activity_id"]:
        return row["repository"], row["activity_id"]
    if row["kind"] == "pull-merged" and row["target"]:
        try:
            key, url = github.linked_source(row["target"])
        except ValueError:
            return None
        if url.rsplit("/", 2)[-2] == "pull":
            repository, number = key.split("#")
            return repository, f"github:pull-merged:{number}"
    return None


def _github_counts(db, bounds, project, actor):
    if not has_event_context(db):
        return 0, False
    visibility, bindings = ring1.visibility(db, "tasks.project_prefix", actor)
    scope = "AND tasks.project_prefix = ?" if project else ""
    rows = db.execute(
        f"""
        WITH activity AS (
            SELECT json_extract(context, '$.provider_at') AS provider_at,
                   json_extract(context, '$.github_repository') AS repository,
                   json_extract(context, '$.github_activity_id') AS activity_id,
                   COALESCE(json_extract(context, '$.github_event'), json_extract(context, '$.event_kind')) AS kind,
                   json_extract(context, '$.github_source') AS target
            FROM events JOIN tasks ON tasks.id = events.task_id
            WHERE (
                (action = 'log' AND actor = ? AND json_extract(context, '$.extension') = ?)
                OR (action = 'source-event' AND actor = 'GitHub' AND json_extract(context, '$.source_kind') = 'github')
            ) AND {visibility} {scope}
        )
        SELECT DISTINCT repository, activity_id, kind, target FROM activity
        WHERE kind <> 'deployed' AND SUBSTR(provider_at, 1, 19) >= ? AND SUBSTR(provider_at, 1, 19) < ?
        """, (extensions.ACTOR, extensions.EVENTS, *bindings, *((project,) if project else ()), *bounds),
    )
    identities = {_github_identity(row) for row in rows}
    older_records = None in identities
    identities.discard(None)
    return len(identities), older_records


def _raft_notices(db, bounds, project, actor):
    if actor is None:
        return {"status": "sign_in", "count": None}
    if not tasks_storage.has_table(db, "notification_log"):
        return {"status": "unavailable", "count": None}
    visibility, bindings = ring1.visibility(db, "tasks.project_prefix", actor)
    scope = "AND tasks.project_prefix = ?" if project else ""
    row = db.execute(
        f"""
        SELECT COUNT(*) AS known,
               COUNT(CASE WHEN SUBSTR(log.send_confirmed_at, 1, 19) >= ? THEN 1 END) AS period_count
        FROM notification_log AS log
        JOIN events ON events.id = log.source_event_id
        JOIN tasks ON tasks.id = events.task_id
        JOIN projects ON projects.prefix = tasks.project_prefix
        WHERE SUBSTR(log.send_confirmed_at, 1, 19) < ?
          AND (projects.prefix IN ({projects.MANAGED_BY}) OR log.recipient = ?) AND {visibility} {scope}
        """, (bounds[0], bounds[1], actor, actor, actor, *bindings, *((project,) if project else ())),
    ).fetchone()
    if not row["known"]:
        return {"status": "unavailable", "count": None}
    if project:
        managed = projects.manages(db, project, actor)
        status = "recorded" if managed else "visible"
    else:
        status = "visible"
    return {"status": status, "count": row["period_count"]}


def snapshot(db, bounds, *, project, actor, github_enabled, project_extensions):
    # Provider times are UTC. Compare whole-second prefixes so each instant on
    # a boundary, including a receipt with fractional seconds, has one window.
    bounds = tuple(at[:19] for at in bounds)
    count, older_records = _github_counts(db, bounds, project, actor)
    configured = (
        [project_extensions.get(project, {})] if project else project_extensions.values()
    )
    automatic = any(options.get(extensions.EVENTS) for options in configured)
    github_status = "recorded" if automatic else ("manual" if github_enabled else "disabled")
    return {
        "github": {
            "status": github_status, "count": count if count or (automatic and not older_records) else None,
            "older_records": older_records,
        },
        "raft_messages": {"status": "unavailable", "count": None},
        "raft_notices": _raft_notices(db, bounds, project, actor),
    }
