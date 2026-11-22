"""Bounded reads for the live board and its agent actions."""

import json
import re
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import tasks_core as ledger
import tasks_goals as goals
import tasks_maintenance as maintenance
import tasks_profiles as profiles
import tasks_projects as projects
import tasks_recurring as recurring
import tasks_releases as releases
import tasks_readiness as readiness
import tasks_storage
import watches
import workers
from tasks_evidence_preview import raster_path


# Older ledger entries do not mark routine replies separately from outcomes.
ROUTINE_ACKNOWLEDGMENT = re.compile(
    r"(?:acknowledged|noted|received|got it|thanks|thank you)[,.!]?"
    r"(?:\s+(?:(?:i|we)(?:['’]ll| will)|will) "
    r"(?:read|take a look|check|follow up|continue(?: with (?:this|the) work)?(?: as planned)?|proceed(?: as planned)?)[.!]?)?",
    re.IGNORECASE,
)
PACIFIC = ZoneInfo("America/Los_Angeles")
TASK_REFERENCE = "tasks.project_prefix || '-' || COALESCE(tasks.project_number, tasks.id)"
ACTIVITY_SELECT = (
    "SELECT events.*, tasks.title AS task_title, " + TASK_REFERENCE
    + " AS task_key FROM events JOIN tasks ON tasks.id = events.task_id"
)
OUTCOME_ACTIONS = "events.action IN ('done', 'decide')"
WORK_STATES = ("human_review", "waiting", "in_progress", "ready")
ACTIVE_STATES = ", ".join(f"'{state}'" for state in WORK_STATES[:-1])
WORK_COUNTS = ", ".join(f"COUNT(CASE WHEN state = '{state}' THEN 1 END) AS {state}" for state in WORK_STATES)
ATTENTION_DUE = "(remind_on IS NULL OR remind_on <= ?)"
ATTENTION_SELECT = f"""
    SELECT attention_requests.*, tasks.title AS task_title, {TASK_REFERENCE} AS task_key,
           blocked.id AS blocked_id, blocked.title AS blocked_title,
           blocked.project_prefix || '-' || COALESCE(blocked.project_number, blocked.id) AS blocked_key,
           wait_links.ready_event_id
    FROM attention_requests JOIN tasks ON tasks.id = attention_requests.task_id
    LEFT JOIN attention_wait_links AS wait_links ON wait_links.request_id = attention_requests.id
    LEFT JOIN events AS wait_event ON wait_event.id = wait_links.event_id
    LEFT JOIN tasks AS blocked ON blocked.id = COALESCE(attention_requests.unblocks_task_id, wait_event.task_id)
"""
RECORDED_START = """
    COALESCE(TRIM(worker_runs.started_at), '') <> ''
    AND COALESCE(TRIM(worker_runs.handle), '') <> ''
"""
RUNNING = f"worker_runs.status = 'running' AND ({RECORDED_START})"
WORKER_COUNTS = f"""
    COUNT(*) AS recorded_runs,
    COUNT(CASE WHEN current.display_state <> 'closed' AND worker_runs.role = 'build' AND ({RUNNING}) THEN 1 END) AS running_builds,
    COUNT(CASE WHEN worker_runs.role = 'build' AND ({RECORDED_START}) THEN 1 END) AS launched_builds,
    COUNT(CASE WHEN current.display_state <> 'closed' AND worker_runs.role = 'review' AND ({RUNNING}) THEN 1 END) AS running_reviews,
    COUNT(CASE WHEN worker_runs.role = 'review' AND ({RECORDED_START}) THEN 1 END) AS launched_reviews
"""
WORKER_STATE = f"""
    CASE WHEN worker_runs.status = 'running' AND NOT ({RECORDED_START}) THEN 'start_unconfirmed'
         ELSE worker_runs.status END
"""
WORKER_ORDER = f"""
    CASE WHEN {RUNNING} THEN 0
         WHEN worker_runs.status = 'reported' THEN 1
         WHEN worker_runs.status = 'planned' THEN 2
         ELSE 3 END
"""


def is_routine_acknowledgment(note):
    return ROUTINE_ACKNOWLEDGMENT.fullmatch(note.strip()) is not None


def pacific_day(at):
    return datetime.fromisoformat(at).astimezone(PACIFIC).date().isoformat()


def page(rows, limit, cursor, render):
    visible = rows[:limit]
    return {
        "items": render(visible),
        "next_cursor": visible[-1][cursor] if len(rows) > limit else None,
    }


def task_creation(event):
    creation = {
        "id": event["id"],
        "task_id": event["task_id"],
        "task_title": event["task_title"],
        "creator": (event["actor_profile"] or {}).get("name", event["actor"]),
        "at": event["at"],
    }
    if not event["task_key"].startswith(f"{projects.DEFAULT}-"):
        creation["task_key"] = event["task_key"]
    return creation


def worker_counts(row):
    return {
        "recorded": row["recorded_runs"],
        "running": row["running_builds"],
        "launched": row["launched_builds"],
        "running_reviews": row["running_reviews"],
        "launched_reviews": row["launched_reviews"],
    }


class BoardQueries:
    def __init__(self, db, base, *, local_links=False, project_roster=None):
        self.db = db
        self.base = base
        self.local_links = local_links
        self.project_roster = project_roster

    def schedule_report(self, number, owner, as_of):
        report = recurring.report(self.db, number, owner, as_of)
        report.pop("key")
        profile = profiles.visible_by_keys(self.db, [owner], self.base).get(owner)
        report["owner_name"] = profile["name"] if profile else "Unknown owner"
        return report

    def schedules(self, project, owner):
        result = {"recurring": [], "maintenance": {"editable": False, "schedule": None, "owner_choices": []}}
        if owner is None:
            return result
        as_of = ledger.timestamp()
        if project == projects.DEFAULT:
            definitions = self.db.execute(
                """
                SELECT id FROM recurring_definitions
                WHERE owner = ? AND id NOT IN (SELECT definition_id FROM project_maintenance)
                ORDER BY id
                """, (owner,),
            )
            result["recurring"] = [self.schedule_report(row["id"], owner, as_of) for row in definitions]
        if projects.manages(self.db, project, owner):
            maintenance_view = result["maintenance"]
            maintenance_view["editable"] = True
            row = maintenance.find(self.db, project=project)
            if row is not None:
                report = self.schedule_report(row["id"], row["owner"], as_of)
                maintenance_view["schedule"] = {**report, **maintenance.describe(row, project)["schedule"]}
                maintenance_view["owner_choices"] = [dict(person) for person in self.db.execute(
                    """
                    SELECT ledger_key AS id, COALESCE(NULLIF(name, ''), NULLIF(username, ''), 'Unknown owner') AS name
                    FROM board_principals ORDER BY name COLLATE NOCASE, ledger_key
                    """,
                )]
        return result

    def links(self, pairs, *, event_id=None):
        values = []
        for index, (label, url) in enumerate(pairs):
            scheme = urlsplit(url).scheme
            allowed = scheme in ("http", "https") or (self.local_links and scheme == "file")
            link = {"label": label, "url": url if allowed else None, "unavailable": not allowed}
            if self.local_links and event_id is not None and raster_path(url) is not None:
                link["preview_url"] = f"./api/v1/local-evidence/{event_id}/{index}"
            values.append(link)
        return values

    def wait_starts(self, rows):
        return ledger.wait_starts(self.db, rows)

    def task_rows(self, rows, *, include_report_fields=False, include_release_results=False):
        if not rows:
            return []
        ids = [row["id"] for row in rows]
        placeholders = ", ".join("?" for _ in ids)
        progress = {
            row["task_id"]: (row["completed"], row["total"])
            for row in self.db.execute(
                f"""
                SELECT task_id, SUM(checked) AS completed, COUNT(*) AS total
                FROM task_steps WHERE task_id IN ({placeholders}) GROUP BY task_id
                """, ids,
            )
        }
        activity_times = {}
        if include_report_fields:
            activity_times = dict(self.db.execute(
                f"""
                SELECT tasks.id, events.at FROM tasks
                JOIN events ON events.id = (
                    SELECT MAX(activity.id) FROM events AS activity WHERE activity.task_id = tasks.id
                )
                WHERE tasks.id IN ({placeholders})
                """, ids,
            ))
        owners = profiles.visible_by_keys(self.db, [row["dri"] for row in rows], self.base)
        recipients = profiles.visible_by_keys(self.db, [row["waiting_on"] for row in rows if row["waiting_on"]], self.base)
        tasks_by_id = {row["id"]: row for row in rows}
        wait_starts = self.wait_starts(rows)

        def lookup(number):
            if number not in tasks_by_id:
                tasks_by_id[number] = ledger.find_task(self.db, number)
            return tasks_by_id[number]

        release_ids = [row["id"] for row in rows if projects.release_queue_enabled(projects.row_project(row))]
        release_fields = releases.ticket_fields(self.db, release_ids)
        release_results = releases.ticket_results(self.db, release_ids) if include_release_results else {}
        results = []
        for row in rows:
            label, note = ledger.current_summary(row)
            complete, total = progress.get(row["id"], (0, 0))
            context = ledger.event_context(row)
            details = context.get("activity_details")
            blockers = ledger.blocker_chain(row["id"], ledger.ticket_blocker(row), lookup)
            item = {
                "id": row["id"],
                "key": projects.row_reference(row),
                "title": row["title"],
                "dri": row["dri"],
                "priority": row["priority"],
                "dri_profile": owners.get(row["dri"]),
                "state": row["state"],
                **({"display_state": "closed"} if row["display_state"] == "closed" else {}),
                "note": note,
                "summary_label": label,
                "waiting_on": row["waiting_on"],
                "waiting_profile": recipients.get(row["waiting_on"]),
                **({"waiting_since": wait_starts[row["id"]]} if row["id"] in wait_starts else {}),
                **({"blockers": blockers} if blockers else {}),
                "context": {
                    key: value for key, value in context.items()
                    if key not in ("activity_details", "eta")
                },
                "links": self.links(json.loads(row["current_links"]), event_id=row["event_id"]),
                "at": row["at"],
                "action": row["current_action"],
                "event_id": row["event_id"],
                "checklist_completed": complete,
                "checklist_total": total,
                **release_fields.get(row["id"], {}),
            }
            if include_report_fields:
                item["last_activity_at"] = activity_times.get(row["id"], row["at"])
                if isinstance(details, str):
                    item["details"] = details
            if receipt := release_results.get(row["id"]):
                item["release_result"] = receipt
                item["release_after_step"] = datetime.fromisoformat(receipt["at"]) > datetime.fromisoformat(row["at"])
            results.append(item)
        return results

    def tasks(self, *, state=None, dri=None, waiting_on=None, before=None, limit=20, now=False,
              decided_by=None, search=None, priority_order=False, review_due=None, project=None, tag=None,
              managed_by=None):
        terms, args = [], []
        filter_search = None
        for column, value in (
            (ledger.display_state_sql("events.context" if ledger.has_event_context(self.db) else "'{}'"), state),
            ("tasks.dri", dri), ("events.waiting_on", waiting_on),
            ("tasks.project_prefix", project),
        ):
            if value is not None:
                terms.append(f"{column} = ?")
                args.append(value)
        if managed_by is not None:
            terms.append("tasks.project_prefix IN (SELECT prefix FROM projects WHERE dri = ?)")
            args.append(managed_by)
        if tag is not None:
            terms.append("tasks.id IN (SELECT task_id FROM task_tags WHERE tag = ?)")
            args.append(tag)
        if now:
            terms.append("tasks.dri <> ''")
            terms.append(
                "(events.state = 'in_progress' OR "
                "(events.state = 'waiting' AND json_extract(events.context, '$.wait_kind') = 'automation'))"
            )
        if decided_by is not None:
            terms.append("events.state = 'ready' AND events.action = 'decide' AND events.actor = ?")
            args.append(decided_by)
        if review_due is not None:
            terms.append("json_extract(events.context, '$.review_after') <= ?")
            args.append(review_due)
        if search is not None:
            needle = search.casefold()
            try:
                task_id = projects.resolve(self.db, search)
            except (ValueError, LookupError):
                task_id = None

            def matches(title, state, action, note, context):
                if needle in title.casefold():
                    return True
                _, summary = ledger.current_summary({
                    "state": state, "current_action": action, "note": note, "context": context,
                })
                return needle in summary.casefold()

            if isinstance(self.db, tasks_storage.Postgres):
                def filter_search(row):
                    return row["id"] == task_id or matches(
                        row["title"], row["state"], row["current_action"], row["note"], row["context"],
                    )
            else:
                self.db.create_function("matches_task_search", 5, matches)
                terms.append("""(tasks.id = ? OR matches_task_search(
                    tasks.title, events.state, events.action, events.note, context
                ))""")
                args.append(task_id)
        order = "events.id DESC"
        if priority_order:
            rank = ledger.PRIORITY_ORDER_SQL
            order = f"{rank}, events.id"
            if before is not None:
                priority, event_id = before
                terms.append(f"({rank}, events.id) > (?, ?)")
                args.extend((ledger.PRIORITIES.index(priority), event_id))
        elif before is not None:
            terms.append("events.id < ?")
            args.append(before)
        if filter_search is None:
            where = " WHERE " + " AND ".join(terms) if terms else ""
            sql = ledger.current_tasks_sql(self.db) + where + f" ORDER BY {order} LIMIT ?"
            rows = self.db.execute(sql, (*args, limit + 1)).fetchall()
        else:
            rows = self.search_rows(terms, args, order, limit, priority_order, filter_search)
        include_report_fields = dri is not None and state in WORK_STATES
        result = page(rows, limit, "event_id", lambda visible: self.task_rows(
            visible, include_report_fields=include_report_fields,
        ))
        if priority_order and result["next_cursor"] is not None:
            last = rows[limit - 1]
            result["next_cursor"] = f"{last['priority']}:{last['event_id']}"
        return result

    def search_rows(self, terms, args, order, limit, priority_order, matches):
        rows = []
        cursor, bindings = [], ()
        batch_size = max(100, limit + 1)
        with tasks_storage.read(self.db):
            base = ledger.current_tasks_sql(self.db)
            while len(rows) <= limit:
                conditions = terms + cursor
                where = " WHERE " + " AND ".join(conditions) if conditions else ""
                batch = self.db.execute(base + where + f" ORDER BY {order} LIMIT ?",
                                        (*args, *bindings, batch_size)).fetchall()
                rows.extend(row for row in batch if matches(row))
                if len(batch) < batch_size:
                    break
                last = batch[-1]
                if priority_order:
                    cursor = [f"({ledger.PRIORITY_ORDER_SQL}, events.id) > (?, ?)"]
                    bindings = (ledger.PRIORITIES.index(last["priority"]), last["event_id"])
                else:
                    cursor, bindings = ["events.id < ?"], (last["event_id"],)
        return rows[:limit + 1]

    def overview(self, *, now_before, decisions_before, limit, recipient, project=projects.DEFAULT):
        counts = dict.fromkeys(ledger.STATES, 0)
        counts.update(self.db.execute(
            "SELECT display_state, COUNT(*) FROM (" + ledger.current_tasks_sql(self.db) +
            " WHERE tasks.project_prefix = ?) GROUP BY display_state", (project,),
        ))
        owners = [row[0] for row in self.db.execute(
            "SELECT DISTINCT dri FROM tasks WHERE project_prefix = ? AND dri <> '' ORDER BY dri LIMIT 256", (project,)
        )]
        return {
            "counts": counts,
            "now": self.tasks(now=True, before=now_before, limit=limit, project=project),
            "decisions": self.tasks(
                state="human_review", waiting_on=recipient, before=decisions_before, limit=limit, project=project,
            ),
            "owners": owners,
            "owner_profiles": profiles.visible_by_keys(self.db, owners, self.base),
            **projects.project_features(project),
        }

    def detail(self, number):
        row = ledger.get_task(self.db, number)
        steps = [dict(step) for step in ledger.checklist_rows(self.db, number)]
        attribution = ("started_by", "completed_by")
        actors = profiles.visible_by_keys(
            self.db, [step[field] for step in steps for field in attribution if step[field]], self.base,
        )
        for step in steps:
            for field in attribution:
                if actor := actors.get(step[field]):
                    step[field + "_name"] = actor.get("name") or actor.get("username")
        sources = []
        for _, label, url, generated in watches.task_source_links(self.db, number):
            link, = self.links([(label, url)])
            if generated:
                link["generated"] = True
            sources.append(link)
        runs = [dict(run) for run in self.db.execute(
            f"""
            SELECT id, worker, role, status, effort, handle, report, result, disposition, brief,
                   assigned_at, started_at, reported_at, finalized_at, {WORKER_STATE} AS execution_state
            FROM worker_runs WHERE task_id = ?
            ORDER BY {WORKER_ORDER}, id DESC
            """, (number,),
        )]
        identities = profiles.visible_by_keys(self.db, [run["worker"] for run in runs], self.base)
        for run in runs:
            run["worker_profile"] = identities.get(run["worker"])
        return {
            "task": {**self.task_rows([row])[0], "eta": row["eta"]},
            "brief": row["brief"],
            "checklist": steps,
            "related_tasks": ledger.related_tasks(self.db, row, steps, self.base + "/"),
            "sources": sources,
            "workers": runs,
            "last_activity": self.last_activity(task_id=number),
        }

    def last_activity(self, *, owner=None, task_id=None, project=None):
        column, value = ("tasks.dri", owner) if owner is not None else ("events.task_id", task_id)
        event = self.db.execute(
            f"""
            SELECT events.id, events.at
            FROM tasks JOIN events ON events.task_id = tasks.id
            WHERE {column} = ? AND (? IS NULL OR tasks.project_prefix = ?) ORDER BY events.id DESC LIMIT 1
            """, (value, project, project),
        ).fetchone()
        return dict(event) if event else None

    def decision_receipt(self, number, request_id, recipient):
        answer = self.db.execute(
            """
            SELECT answer.id AS event_id, answer.at, answer.note,
                   json_extract(answer.context, '$.choice') AS choice
            FROM events AS original
            LEFT JOIN events AS answer
              ON answer.task_id = original.task_id AND answer.actor = original.waiting_on
             AND answer.id > original.id AND answer.action = 'decide'
             AND json_extract(answer.context, '$.request_event_id') = original.id
            WHERE original.task_id = ? AND original.id = ? AND original.waiting_on = ?
              AND original.action IN ('assign-human', 'redirect-decision')
            ORDER BY answer.id LIMIT 1
            """,
            (number, request_id, recipient),
        ).fetchone()
        if answer is None:
            return None
        if answer["event_id"] is not None:
            return {"status": "answered", "answer": dict(answer)}
        current = ledger.find_task(self.db, number)
        active = current is not None and current["event_id"] == request_id and current["state"] == "human_review"
        return {"status": "open" if active else "closed", "answer": None}

    def work_event_kinds(self, rows):
        logs = {row["id"]: row for row in rows if row["action"] == "log"}
        if not logs:
            return {}
        marks = ", ".join("?" for _ in logs)
        events = {}
        for row in self.db.execute(
            f"""
            SELECT review_event_id, status_event_id, status
            FROM merge_candidates
            WHERE review_event_id IN ({marks}) OR status_event_id IN ({marks})
            """,
            (*logs, *logs),
        ):
            if row["review_event_id"] is not None:
                events[row["review_event_id"]] = "merge-review"
            if row["status"] == "merged":
                events[row["status_event_id"]] = "local-merge"
        receipt_ids = {f"{self.base}/#event-{event_id}-log": event_id for event_id in logs}
        for receipt in self.db.execute(
            f"""
            SELECT release_events.*, release_batches.release_id, release_batches.candidate
            FROM release_events
            JOIN release_batches ON release_batches.id = release_events.batch_id
            WHERE receipt_url IN ({marks})
            AND status IN ('released', 'rolled_back', 'rollback_failed', 'rejected')
            """,
            tuple(receipt_ids),
        ):
            event_id = receipt_ids[receipt["receipt_url"]]
            event = logs[event_id]
            if event["task_id"] != releases.activity_task(self.db, receipt):
                continue
            details = ledger.event_context(event).get("activity_details")
            if releases.matches_activity_outcome(details, event["task_key"], receipt):
                events[event_id] = "release-" + receipt["status"]
        return events

    def event_rows(self, rows):
        work_kinds = self.work_event_kinds(rows)
        keys = [row["actor"] for row in rows] + [row["waiting_on"] for row in rows if row["waiting_on"]]
        people = profiles.visible_by_keys(self.db, keys, self.base)
        events = []
        for stored in rows:
            row = watches.display_event(stored)
            context = ledger.event_context(row)
            details = context.pop("activity_details", None)
            if row["id"] in work_kinds:
                context["work_kind"] = work_kinds[row["id"]]
            actor_profile = people.get(row["actor"]) if row["actor"] == stored["actor"] else None
            event = {
                **{key: row[key] for key in (
                    "id", "task_id", "task_key", "task_title", "at", "actor", "action", "state", "note", "waiting_on",
                )},
                "label": ledger.event_label(row["action"], context),
                "context": context,
                "links": self.links(json.loads(row["links"]), event_id=row["id"]),
                "actor_profile": actor_profile,
                "waiting_profile": people.get(row["waiting_on"]),
                "related_tasks": ledger.resolve_task_references(
                    self.db, row["task_id"], (row["note"], details), self.base + "/",
                ),
            }
            if details is not None:
                event.update(headline=row["note"], details=details or None)
            events.append(event)
        return events

    def outcome_day_sql(self, time_zone):
        if isinstance(self.db, tasks_storage.Postgres):
            self.db.execute("SELECT set_config('TimeZone', ?, false)", (time_zone.key,))
            return "CAST(CAST(CAST(events.at AS timestamptz) AS date) AS text)"
        self.db.create_function(
            "outcome_day", 1,
            lambda at: datetime.fromisoformat(at).astimezone(time_zone).date().isoformat(),
            deterministic=True,
        )
        return "outcome_day(events.at)"

    def events(self, *, number=None, event_id=None, actor=None, action=None, before=None,
               limit=20, outcomes=False, watcher=None, owner_or_watcher=None,
               dri=None, feed=None, day=None, project=None, time_zone=PACIFIC):
        joins, terms, args = "", [], []
        if watcher is not None:
            joins = " JOIN task_watches ON task_watches.task_id = events.task_id"
            terms.extend(("task_watches.watcher = ?", "events.id > task_watches.after_event"))
            args.append(watcher)
        if owner_or_watcher is not None:
            terms.append(
                """(tasks.dri = ? OR EXISTS (
                    SELECT 1 FROM task_watches
                    WHERE task_watches.task_id = tasks.id AND task_watches.watcher = ?
                      AND events.id > task_watches.after_event
                ))"""
            )
            terms.append("(events.action <> 'source-event' OR events.actor IN ('GitHub', 'TaskNote'))")
            args.extend((owner_or_watcher, owner_or_watcher))
        if actor == watches.LOCAL_FILE:
            terms.append("(events.actor = ? OR (events.action = 'source-event' AND events.actor = 'TaskNote'))")
            args.append(actor)
            actor = None
        for column, value in (
            ("events.task_id", number), ("events.id", event_id), ("events.actor", actor),
            ("events.action", action), ("tasks.dri", dri), ("tasks.project_prefix", project),
        ):
            if value is not None:
                terms.append(f"{column} = ?")
                args.append(value)
        if before is not None:
            terms.append("events.id < ?")
            args.append(before)
        if day is not None:
            day_sql = self.outcome_day_sql(time_zone)
            terms.append(f"{day_sql} = ?")
            args.append(day.isoformat())
        if outcomes:
            terms.append(OUTCOME_ACTIONS)
        elif feed == "outcomes":
            if isinstance(self.db, tasks_storage.Postgres):
                acknowledge = r"(regexp_replace(events.note, '^\s+|\s+$', '', 'g') ~* ?)"
                args.append("^" + ROUTINE_ACKNOWLEDGMENT.pattern + "$")
            else:
                self.db.create_function("routine_acknowledgment", 1, is_routine_acknowledgment, deterministic=True)
                acknowledge = "routine_acknowledgment(events.note)"
            terms.append(
                "(events.action IN ('done', 'decide', 'wait', 'resume', 'assign-human', "
                "'redirect-decision', 'withdraw-decision', 'transfer', 'park', 'unpark', 'source-event') "
                "OR (events.action IN ('log', 'update') AND ("
                "json_extract(events.context, '$.ownership_action') IS NOT NULL "
                "OR json_type(events.context, '$.closure') = 'object' "
                "OR json_extract(events.context, '$.worker_report_id') IS NOT NULL "
                "OR json_extract(events.context, '$.worker_disposition_id') IS NOT NULL "
                "OR EXISTS (SELECT 1 FROM merge_candidates AS merge "
                "WHERE merge.review_event_id = events.id OR (merge.status = 'merged' AND merge.status_event_id = events.id)) "
                "OR ((json_array_length(events.links) > 0 "
                "OR json_type(events.context, '$.activity_details') = 'text') "
                f"AND NOT {acknowledge}))))"
            )
        elif (feed != "all" and action is None and number is None and event_id is None
              and watcher is None and owner_or_watcher is None):
            routine = ", ".join("?" for _ in ledger.ROUTINE_AUDIT_ACTIONS)
            terms.append(
                "events.action NOT IN ('add', 'start') "
                f"AND COALESCE(json_extract(events.context, '$.audit.action'), '') NOT IN ({routine})"
            )
            args.extend(ledger.ROUTINE_AUDIT_ACTIONS)
        where = " WHERE " + " AND ".join(terms) if terms else ""
        sql = ACTIVITY_SELECT + joins + where + " ORDER BY events.id DESC LIMIT ?"
        rows = self.db.execute(sql, (*args, limit + 1)).fetchall()
        return page(rows, limit, "id", self.event_rows)

    def outcome_days(self, *, dri, before, limit, project=None, time_zone=PACIFIC):
        day_sql = self.outcome_day_sql(time_zone)
        terms, args = [OUTCOME_ACTIONS], []
        if dri is not None:
            terms.append("tasks.dri = ?")
            args.append(dri)
        if project is not None:
            terms.append("tasks.project_prefix = ?")
            args.append(project)
        if before is not None:
            terms.append(f"{day_sql} < ?")
            args.append(before.isoformat())
        where = " AND ".join(terms)
        days = [
            row[0] for row in self.db.execute(
                f"SELECT DISTINCT {day_sql} AS day "
                "FROM events JOIN tasks ON tasks.id = events.task_id "
                f"WHERE {where} ORDER BY day DESC LIMIT ?", (*args, limit + 1),
            )
        ]
        return {"days": days[:limit], "next_day": days[limit - 1] if len(days) > limit else None}

    def related_activity(self, person, *, before, limit, project=None):
        result = self.events(before=before, limit=limit, owner_or_watcher=person, project=project)
        failures = self.db.execute(
            """
            SELECT sources.source_key, intake_errors.error, intake_errors.at
            FROM intake_errors JOIN sources ON sources.id = intake_errors.source_id
            WHERE EXISTS (
                SELECT 1 FROM task_sources
                JOIN tasks ON tasks.id = task_sources.task_id
                LEFT JOIN task_watches ON task_watches.task_id = tasks.id AND task_watches.watcher = ?
                WHERE task_sources.source_id = sources.id AND (tasks.dri = ? OR task_watches.watcher IS NOT NULL)
                    AND (? IS NULL OR tasks.project_prefix = ?)
            )
            ORDER BY intake_errors.at DESC, sources.source_key LIMIT 20
            """, (person, person, project, project),
        )
        return {**result, "source_errors": [dict(error) for error in failures]}

    def owner_workers(self, owner, *, before, limit, order_execution=False, project=None):
        order = "worker_runs.id DESC"
        cursor, parameters = "worker_runs.id < ?", (before or 2**63 - 1,)
        if order_execution:
            order = f"{WORKER_ORDER}, worker_runs.id DESC"
            cursor, parameters = "", ()
            if before is not None:
                rank, run_id = before
                cursor = f"({WORKER_ORDER}, -worker_runs.id) > (?, ?)"
                parameters = (rank, -run_id)
        where = f" AND {cursor}" if cursor else ""
        rows = self.db.execute(
            f"""
            SELECT worker_runs.id, worker_runs.worker, worker_runs.role, worker_runs.status,
                   worker_runs.brief, worker_runs.effort, worker_runs.result, worker_runs.assigned_at, worker_runs.started_at,
                   worker_runs.reported_at, worker_runs.finalized_at, {WORKER_STATE} AS execution_state,
                   {WORKER_ORDER} AS execution_rank,
                   COALESCE(worker_runs.finalized_at, worker_runs.reported_at,
                            worker_runs.started_at, worker_runs.assigned_at) AS updated_at,
                   tasks.id AS task_id, tasks.title AS task_title, {TASK_REFERENCE} AS task_key
            FROM worker_runs JOIN tasks ON tasks.id = worker_runs.task_id
            WHERE tasks.dri = ? AND (? IS NULL OR tasks.project_prefix = ?){where}
            ORDER BY {order} LIMIT ?
            """, (owner, project, project, *parameters, limit + 1),
        ).fetchall()

        def render(visible):
            people = profiles.visible_by_keys(self.db, [row["worker"] for row in visible], self.base)
            return [{**{key: row[key] for key in row.keys() if key != "execution_rank"},
                     "worker_profile": people.get(row["worker"])} for row in visible]

        result = page(rows, limit, "id", render)
        if order_execution and result["next_cursor"] is not None:
            last = rows[limit - 1]
            result["next_cursor"] = f"{last['execution_rank']}:{last['id']}"
        return result

    def actionable_reviews(self, person, *, before, limit, project=None):
        rows = self.db.execute(
            f"""
            WITH current AS ({ledger.current_tasks_sql(self.db)})
            SELECT worker_runs.id, worker_runs.status, worker_runs.brief,
                   tasks.id AS task_id, tasks.title AS task_title, {TASK_REFERENCE} AS task_key
            FROM worker_runs JOIN tasks ON tasks.id = worker_runs.task_id
            JOIN current ON current.id = tasks.id
            WHERE worker_runs.worker = ? AND worker_runs.role = 'review'
                AND worker_runs.status IN ('planned', 'running') AND worker_runs.id < ?
                AND current.display_state <> 'closed'
                AND (? IS NULL OR tasks.project_prefix = ?)
            ORDER BY worker_runs.id DESC LIMIT ?
            """, (person, before or projects.MAX_ID, project, project, limit + 1),
        ).fetchall()
        return page(rows, limit, "id", lambda visible: [dict(row) for row in visible])

    def queued_reviews(self, *, before, limit, project=projects.DEFAULT):
        rows = self.db.execute(
            f"""
            WITH current AS ({ledger.current_tasks_sql(self.db)})
            SELECT worker_runs.id, worker_runs.task_id, worker_runs.worker, worker_runs.status,
                   tasks.title AS task_title, {TASK_REFERENCE} AS task_key, {WORKER_STATE} AS execution_state
            FROM worker_runs JOIN tasks ON tasks.id = worker_runs.task_id
            JOIN current ON current.id = tasks.id
            WHERE worker_runs.role = 'review' AND worker_runs.status IN ('planned', 'running')
              AND current.display_state <> 'closed'
              AND tasks.project_prefix = ? AND worker_runs.id < ?
            ORDER BY worker_runs.id DESC LIMIT ?
            """, (project, before or projects.MAX_ID, limit + 1),
        ).fetchall()

        def render(visible):
            recipients = profiles.visible_by_keys(self.db, [row["worker"] for row in visible], self.base)
            return [{
                "id": row["id"], "task_id": row["task_id"], "task_title": row["task_title"],
                "task_key": row["task_key"],
                "recipient": recipients.get(row["worker"], {}).get("name") or row["worker"],
                "state": row["status"],
                "execution_state": row["execution_state"],
            } for row in visible]

        return page(rows, limit, "id", render)

    def queued_peer_waits(self, *, before, limit, project=projects.DEFAULT):
        rows = self.db.execute(
            f"""
            WITH current AS ({ledger.current_tasks_sql(self.db)} WHERE tasks.project_prefix = ?)
            SELECT tasks.event_id AS id, tasks.id AS task_id, tasks.title AS task_title,
                   {TASK_REFERENCE} AS task_key,
                   COALESCE(NULLIF(peer.name, ''), NULLIF(peer.username, ''), peer.ledger_key) AS recipient
            FROM current AS tasks
            JOIN board_principals AS peer ON peer.ledger_key = COALESCE(
                (SELECT ledger_key FROM board_principals WHERE ledger_key = tasks.waiting_on),
                (SELECT MIN(ledger_key) FROM board_principals
                 WHERE username <> '' AND LOWER(username) = LOWER(tasks.waiting_on)
                 HAVING COUNT(*) = 1)
            )
            WHERE tasks.state = 'waiting' AND tasks.event_id < ?
              AND peer.kind = 'agent' AND peer.ledger_key <> tasks.dri
              AND COALESCE(json_extract(tasks.context, '$.wait_kind'), '') <> 'automation'
              AND json_extract(tasks.context, '$.blocked_by') IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM worker_runs
                  WHERE task_id = tasks.id AND worker = peer.ledger_key AND role = 'review'
                    AND status IN ('planned', 'running')
              )
            ORDER BY tasks.event_id DESC LIMIT ?
            """, (project, before or projects.MAX_ID, limit + 1),
        ).fetchall()
        return page(rows, limit, "id", lambda visible: [dict(row) for row in visible])

    def recent_report_outcomes(self, owner, project):
        # Read each action in event ID order before combining the small results.
        recent_ids = """
            SELECT events.id FROM events JOIN tasks ON tasks.id = events.task_id
            WHERE events.action = ? AND tasks.dri = ? AND tasks.project_prefix = ?
            ORDER BY events.id DESC LIMIT 3
        """
        rows = self.db.execute(
            f"""
            WITH completions AS ({recent_ids}), decisions AS ({recent_ids})
            {ACTIVITY_SELECT}
            WHERE events.id IN (SELECT id FROM completions UNION ALL SELECT id FROM decisions)
            ORDER BY events.id DESC LIMIT 3
            """, ("done", owner, project, "decide", owner, project),
        ).fetchall()
        return self.event_rows(rows)

    def reports(self, *, view, before, limit, project=projects.DEFAULT):
        current = ledger.current_tasks_sql(self.db) + " WHERE tasks.project_prefix = ?"
        work_columns = ", ".join(f"COALESCE(work.{state}, 0) AS {state}" for state in WORK_STATES)
        roster = project == projects.DEFAULT and self.project_roster is not None
        names = projects.roster_owners(self.db, self.project_roster) if roster else {}
        if roster and not names:
            return {"items": [], "next_cursor": None}
        owner_source = """SELECT dri FROM project_tasks WHERE dri <> ''
                          UNION SELECT ledger_key FROM board_principals WHERE kind = 'agent' AND ? = 'T'
                          UNION SELECT dri FROM projects WHERE prefix = ? AND dri IS NOT NULL"""
        owner_args = (project, project)
        if roster:
            owner_source = "VALUES " + ", ".join("(?)" for _ in names)
            owner_args = tuple(names)
        order = "cursor" if view == "all" else "dri"
        filter_args = () if view == "all" else (view == "active",)
        view_filter = "" if view == "all" else " AND is_active = ?"
        rows = self.db.execute(
            f"""
            WITH current AS ({current}),
                 project_tasks AS (SELECT id, dri FROM tasks WHERE project_prefix = ?),
                 work AS (
                     SELECT dri, {WORK_COUNTS},
                            COUNT(CASE WHEN state IN ({ACTIVE_STATES}) THEN 1 END) AS task_count,
                            MAX(CASE WHEN state IN ({ACTIVE_STATES}) THEN at END) AS updated
                     FROM current GROUP BY dri
                 ),
                 reports AS (
                     SELECT current.dri, COUNT(*) AS report_count, MAX(worker_runs.reported_at) AS updated
                     FROM worker_runs JOIN current ON current.id = worker_runs.task_id
                     WHERE worker_runs.status = 'reported' AND current.display_state <> 'closed'
                     GROUP BY current.dri
                 ),
                 runs AS (
                     SELECT current.dri, {WORKER_COUNTS}
                     FROM worker_runs JOIN current ON current.id = worker_runs.task_id
                     GROUP BY current.dri
                 ),
                 owners(dri) AS ({owner_source}),
                 summary AS (
                     SELECT owners.dri, {work_columns}, COALESCE(work.task_count, 0) AS task_count,
                            COALESCE(reports.report_count, 0) AS report_count,
                            COALESCE(runs.recorded_runs, 0) AS recorded_runs,
                            COALESCE(runs.running_builds, 0) AS running_builds,
                            COALESCE(runs.launched_builds, 0) AS launched_builds,
                            COALESCE(runs.running_reviews, 0) AS running_reviews,
                            COALESCE(runs.launched_reviews, 0) AS launched_reviews,
                            NULLIF({tasks_storage.greatest(self.db)}(
                                COALESCE(work.updated, ''), COALESCE(reports.updated, '')
                            ), '') AS as_of,
                            (COALESCE(work.task_count, 0) > 0 OR reports.dri IS NOT NULL
                                OR COALESCE(runs.running_builds, 0) > 0
                                OR COALESCE(runs.running_reviews, 0) > 0) AS is_active
                     FROM owners
                     LEFT JOIN work ON work.dri = owners.dri
                     LEFT JOIN reports ON reports.dri = owners.dri
                     LEFT JOIN runs ON runs.dri = owners.dri
                 ),
                 ordered AS (
                     SELECT summary.*, (CASE WHEN is_active THEN '0:' ELSE '1:' END) || dri AS cursor
                     FROM summary
                 )
            SELECT * FROM ordered WHERE {order} > ?{view_filter}
            ORDER BY {order} LIMIT ?
            """, (project, project, *owner_args, before or "", *filter_args, limit + 1),
        ).fetchall()

        def render(visible):
            people = [row["dri"] for row in visible]
            directory = profiles.visible_by_keys(self.db, people, self.base)
            goal_summaries = goals.report_summaries(self.db, project, people)
            results = []
            for row in visible:
                person = row["dri"]
                active = []
                if row["task_count"]:
                    active = self.db.execute(
                        current + f" AND tasks.dri = ? AND events.state IN ({ACTIVE_STATES}) "
                        "ORDER BY CASE events.state WHEN 'human_review' THEN 0 WHEN 'waiting' THEN 1 ELSE 2 END, "
                        "events.id DESC LIMIT 10",
                        (project, person),
                    ).fetchall()
                pending = self.pending_reports(person, project=project) if row["report_count"] else {"items": [], "next_cursor": None}
                profile = directory.get(person)
                if profile is None and person in names:
                    profile = projects.roster_profile(person, names[person])
                owned = self.task_rows(active, include_report_fields=True, include_release_results=True)
                for task in owned:
                    if task["state"] == "human_review":
                        # Pending questions are available in the recipient's notifications.
                        task.pop("details", None)
                        task.update(note="", waiting_on=None, waiting_profile=None, context={}, links=[], event_id=None)
                results.append({
                    "owner": person,
                    "profile": profile,
                    "goals": goal_summaries.get(person, []),
                    "as_of": row["as_of"],
                    "last_activity": self.last_activity(owner=person, project=project),
                    "tasks": owned,
                    "task_count": row["task_count"],
                    "task_counts": {state: row[state] for state in WORK_STATES},
                    "recent_outcomes": self.recent_report_outcomes(person, project),
                    "worker_counts": worker_counts(row),
                    "pending_reports": pending["items"],
                    "pending_reports_before": pending["next_cursor"],
                    "report_count": row["report_count"],
                })
            return results

        return page(rows, limit, order, render)

    def assignee(self, owner, *, project=projects.DEFAULT):
        profile = profiles.visible_by_keys(self.db, [owner], self.base).get(owner)
        if profile is None and project == projects.DEFAULT and self.project_roster:
            name = projects.roster_owners(self.db, self.project_roster).get(owner)
            if name:
                profile = projects.roster_profile(owner, name)
        if profile is None and not self.db.execute("SELECT 1 FROM tasks WHERE dri = ? LIMIT 1", (owner,)).fetchone():
            return None
        current = ledger.current_tasks_sql(self.db)
        count = self.db.execute(
            f"""
            WITH current AS ({current})
            SELECT COUNT(*) FROM worker_runs JOIN current ON current.id = worker_runs.task_id
            WHERE current.dri = ? AND current.project_prefix = ?
              AND worker_runs.status = 'reported' AND current.display_state <> 'closed'
            """, (owner, project),
        ).fetchone()[0]
        reports = self.pending_reports(owner, project=project)
        counts = self.db.execute(
            f"""
            WITH current AS ({current})
            SELECT {WORKER_COUNTS}
            FROM worker_runs JOIN current ON current.id = worker_runs.task_id
            WHERE current.dri = ? AND current.project_prefix = ?
            """, (owner, project),
        ).fetchone()
        task_counts = dict(self.db.execute(
            f"SELECT {WORK_COUNTS} FROM ({current} WHERE tasks.dri = ? AND tasks.project_prefix = ?)",
            (owner, project),
        ).fetchone())
        has_outcomes = self.db.execute(
            "SELECT 1 FROM events JOIN tasks ON tasks.id = events.task_id "
            f"WHERE tasks.dri = ? AND tasks.project_prefix = ? AND {OUTCOME_ACTIONS} LIMIT 1", (owner, project),
        ).fetchone() is not None
        return {
            "profile": profile, "report_count": count,
            "goal": goals.get(self.db, project, owner),
            "milestones": goals.milestones(self.db, project, owner),
            "standing_goal": next(iter(goals.standing(self.db, owner, project)), None),
            "pending_reports": reports["items"], "pending_reports_before": reports["next_cursor"],
            "in_progress": task_counts["in_progress"] > 0,
            "task_counts": task_counts,
            "has_outcomes": has_outcomes,
            "worker_counts": worker_counts(counts),
        }

    def pending_reports(self, person, *, before=None, limit=10, project=None):
        runs = self.db.execute(
            f"""
            WITH current AS ({ledger.current_tasks_sql(self.db)})
            SELECT worker_runs.*, tasks.title AS task_title, {TASK_REFERENCE} AS task_key
            FROM worker_runs JOIN tasks ON tasks.id = worker_runs.task_id
            JOIN current ON current.id = tasks.id
            WHERE tasks.dri = ? AND worker_runs.status = 'reported'
                AND current.display_state <> 'closed'
                AND worker_runs.id < ? AND (? IS NULL OR tasks.project_prefix = ?)
            ORDER BY worker_runs.id DESC LIMIT ?
            """, (person, before or 2**63 - 1, project, project, limit + 1),
        ).fetchall()

        return page(runs, limit, "id", self.report_rows)

    def report_rows(self, runs):
        directory = profiles.visible_by_keys(self.db, [run["worker"] for run in runs], self.base)
        results = []
        for run in runs:
            events = self.db.execute(
                """
                SELECT * FROM events
                WHERE task_id = ? AND action = 'log' AND actor = ?
                    AND (json_extract(context, '$.worker_report_id') = ? OR note = ?)
                ORDER BY id DESC
                """, (run["task_id"], run["worker"], run["id"], workers.report_note(run)),
            )
            event = workers.report_activity(run, events)
            results.append({
                **{key: run[key] for key in ("task_id", "task_key", "task_title", "worker", "report", "result", "reported_at")},
                "run_id": run["id"],
                "worker_profile": directory.get(run["worker"]),
                "event_id": event["id"] if event is not None else None,
                "links": self.links(json.loads(event["links"]), event_id=event["id"]) if event is not None else [],
            })
        return results

    def open_notification_count(self, recipient, *, today, project=None):
        return self.db.execute(
            f"""
            WITH current_tasks AS ({ledger.current_tasks_sql(self.db)}),
            visible_tasks AS (SELECT id FROM tasks WHERE ? IS NULL OR project_prefix = ?)
            SELECT
                (SELECT COUNT(*) FROM current_tasks JOIN visible_tasks ON visible_tasks.id = current_tasks.id
                 WHERE state = 'human_review' AND waiting_on = ?)
                +
                (SELECT COUNT(*) FROM attention_requests JOIN visible_tasks ON visible_tasks.id = attention_requests.task_id
                 WHERE recipient = ? AND handled_at IS NULL AND {ATTENTION_DUE}) AS open_count
            """, (project, project, recipient, recipient, today),
        ).fetchone()["open_count"]

    def inbox(self, recipient, *, creations, today, before, limit, project=None, include_actions=True):
        current_tasks = ledger.current_tasks_sql(self.db)
        args = [project, project, recipient, project, project, creations, recipient,
                include_actions, recipient, include_actions, recipient, today]
        cursor_filter = ""
        if before is not None:
            cursor_filter = "WHERE (at, source, id) < (?, ?, ?)"
            args.extend(before)
        rows = self.db.execute(
            f"""
            WITH current_tasks AS ({current_tasks}),
            visible_tasks AS (SELECT id FROM tasks WHERE ? IS NULL OR project_prefix = ?),
            {readiness.CANDIDATES},
            candidates AS (
                SELECT at, 'event' AS source, events.id, 'creation' AS kind
                FROM events JOIN visible_tasks ON visible_tasks.id = events.task_id
                WHERE action = 'add' AND ?
                UNION ALL
                SELECT events.at, 'event', events.id, 'watched'
                FROM task_watches JOIN events ON events.task_id = task_watches.task_id
                JOIN visible_tasks ON visible_tasks.id = events.task_id
                WHERE task_watches.watcher = ? AND events.id > task_watches.after_event
                  AND events.action NOT IN ('add', 'assign-human', 'redirect-decision', 'withdraw-decision', 'decide')
                UNION ALL
                SELECT at, 'decision', event_id, 'decision'
                FROM current_tasks JOIN visible_tasks ON visible_tasks.id = current_tasks.id
                WHERE ? AND state = 'human_review' AND waiting_on = ?
                UNION ALL
                SELECT raised_at, 'request', attention_requests.id, 'request'
                FROM attention_requests JOIN visible_tasks ON visible_tasks.id = attention_requests.task_id
                WHERE ? AND recipient = ? AND handled_at IS NULL AND {ATTENTION_DUE}
                UNION ALL
                SELECT at, 'readiness', id, 'readiness' FROM readiness_pending
            )
            SELECT * FROM candidates {cursor_filter}
            ORDER BY at DESC, source DESC, id DESC LIMIT ?
            """, (*args, limit + 1),
        ).fetchall()
        visible = rows[:limit]
        by_source = {
            "event": self.event_rows,
            "decision": self.task_rows,
            "request": self.attention_rows,
            "readiness": None,
        }
        payloads = {}
        for source, render in by_source.items():
            ids = [row["id"] for row in visible if row["source"] == source]
            if not ids:
                continue
            placeholders = ", ".join("?" for _ in ids)
            if source == "readiness":
                sql = readiness.query(current_tasks) + f" WHERE id IN ({placeholders})"
                records = self.db.execute(sql, (recipient, project, project, *ids))
                payloads.update((("readiness", row["id"]), readiness.item(row)) for row in records)
                continue
            if source == "event":
                sql = (f"SELECT events.*, tasks.title AS task_title, {TASK_REFERENCE} AS task_key "
                       "FROM events JOIN tasks ON tasks.id = events.task_id "
                       f"WHERE events.id IN ({placeholders})")
            elif source == "decision":
                sql = current_tasks + f" WHERE events.id IN ({placeholders})"
            else:
                sql = ATTENTION_SELECT + f" WHERE attention_requests.id IN ({placeholders})"
            records = render(self.db.execute(sql, ids).fetchall())
            id_field = "event_id" if source == "decision" else "id"
            payloads.update({(source, item[id_field]): item for item in records})
        items = []
        for row in visible:
            item = payloads[(row["source"], row["id"])]
            if row["kind"] == "creation":
                item = task_creation(item)
            items.append({"kind": row["kind"], "id": row["id"], "at": row["at"], "item": item})
        return {
            "items": items,
            "next_cursor": json.dumps([visible[-1][field] for field in ("at", "source", "id")])
            if len(rows) > limit else None,
        }

    def actionable_work(self, owner, *, today, project):
        """Yield the first actionable item in each queue in pickup order."""
        scope = {"project": project, "limit": 1}
        queues = (
            ("ready", self.tasks, {"dri": owner, "state": "ready", "priority_order": True}),
            ("review", self.actionable_reviews, {"person": owner, "before": None}),
            ("report", self.pending_reports, {"person": owner}),
            ("readiness", self.readiness, {"owner": owner, "before": None}),
            ("request", self.attention, {"recipient": owner, "state": "open", "today": today, "before": None}),
            ("decision", self.tasks, {"waiting_on": owner, "state": "human_review"}),
            ("reminder", self.tasks, {"dri": owner, "state": "parked", "review_due": today}),
            ("in_progress", self.tasks, {"dri": owner, "state": "in_progress"}),
            ("shared_ready", self.tasks, {"dri": "", "state": "ready", "priority_order": True, "managed_by": owner}),
        )
        for kind, query, filters in queues:
            for item in query(**scope, **filters)["items"]:
                yield {
                    "kind": kind,
                    "id": item.get("task_id", item.get("id")),
                    "key": item.get("task_key") or item["key"],
                    "title": item.get("task_title") or item["title"],
                }

    def readiness(self, owner, *, project, before, limit):
        return readiness.page(self.db, ledger.current_tasks_sql(self.db), owner, project, before, limit)

    def attention_rows(self, rows):
        senders = profiles.visible_by_keys(self.db, [row["sender"] for row in rows], self.base)
        return [{
            **{key: row[key] for key in (
                "id", "task_id", "task_key", "task_title", "sender", "reason", "raised_at", "handled_at", "remind_on",
            )},
            "sender_profile": senders.get(row["sender"]),
            "links": self.links(json.loads(row["links"])),
            "unblocks": {
                "id": row["blocked_id"], "key": row["blocked_key"], "title": row["blocked_title"],
                "ready_event_id": row["ready_event_id"],
            } if row["blocked_id"] is not None else None,
        } for row in rows]

    def attention(self, recipient, *, state, today, before, limit, task_id=None, project=None):
        task_filter = "AND tasks.id = ?" if task_id is not None else ""
        args = [recipient, state == "handled", before or 2**63 - 1, project, project]
        date_filter = ""
        if state == "deferred":
            date_filter = "AND remind_on > ?"
            args.append(today)
        elif state == "open":
            date_filter = f"AND {ATTENTION_DUE}"
            args.append(today)
        if task_id is not None:
            args.append(task_id)
        rows = self.db.execute(
            f"""
            {ATTENTION_SELECT}
            WHERE recipient = ? AND (handled_at IS NOT NULL) = ? AND attention_requests.id < ?
            AND (? IS NULL OR tasks.project_prefix = ?)
            {date_filter}
            {task_filter}
            ORDER BY attention_requests.id DESC LIMIT ?
            """, (*args, limit + 1),
        ).fetchall()

        return page(rows, limit, "id", self.attention_rows)

    def milestones(self, *, before, limit, project=None):
        rows = self.db.execute(
            """
            SELECT task_id, workstream, prerequisites
            FROM milestones JOIN tasks ON tasks.id = milestones.task_id
            WHERE task_id < ? AND (? IS NULL OR tasks.project_prefix = ?)
            ORDER BY task_id DESC LIMIT ?
            """, (before or 2**63 - 1, project, project, limit + 1),
        ).fetchall()

        def render(visible):
            references = {row["task_id"]: json.loads(row["prerequisites"]) for row in visible}
            ids = list(dict.fromkeys(number for task, deps in references.items() for number in (task, *deps)))
            tasks = {}
            for start in range(0, len(ids), 100):
                chunk = ids[start:start + 100]
                marks = ", ".join("?" for _ in chunk)
                related = self.db.execute(
                    ledger.current_tasks_sql(self.db) + f" WHERE tasks.id IN ({marks}) AND (? IS NULL OR tasks.project_prefix = ?)",
                    (*chunk, project, project),
                ).fetchall()
                tasks.update({task["id"]: task for task in self.task_rows(related)})
            return [{
                "task": tasks[row["task_id"]],
                "workstream": row["workstream"],
                "prerequisites": [tasks[number] for number in references[row["task_id"]] if number in tasks],
            } for row in visible]

        return page(rows, limit, "task_id", render)
