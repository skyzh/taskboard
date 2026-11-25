"""Bounded reads for the live board and its agent actions."""

import json
import re
from collections import deque
from contextlib import nullcontext
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from heapq import nsmallest
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import tasks_core as ledger
import tasks_goal_offers as goal_offers
import tasks_goals as goals
import tasks_knowledge as knowledge
import tasks_maintenance as maintenance
import tasks_profiles as profiles
import tasks_projects as projects
import tasks_peer_asks as peer_asks
import tasks_recurring as recurring
import tasks_releases as releases
import tasks_readiness as readiness
import tasks_storage
import watches
import workers
from taskboard_server import local_dri
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
SUGGESTION_KEY = re.compile(r"([A-Z][A-Z0-9]{0,7})-([0-9]*)", re.ASCII)
SEARCH_WORDS = re.compile(r"[^\W_]+")
ACTIVITY_EXCERPT_LIMIT = 180
GOAL_OUTCOME_LIMIT = 20
DECISION_RECORD_TAG = knowledge.DECISION_TAG
GITHUB_SOURCE_MATCH = """
    tasks.id IN (
        SELECT task_sources.task_id FROM sources
        JOIN task_sources ON task_sources.source_id = sources.id
        WHERE sources.kind = 'github' AND LOWER(sources.target) IN (?, ?)
    )
"""


def title_suggestion_rank(needle, words, title):
    if needle in title:
        return 2
    if not words:
        return None
    title_words = SEARCH_WORDS.findall(title)
    if all(any(candidate.startswith(word) or (
        len(word) >= 4 and SequenceMatcher(None, word, candidate).ratio() >= 0.8
    ) for candidate in title_words) for word in words):
        return 3
    return None

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
REVIEW_REQUESTER = """
    SELECT events.actor FROM events
    WHERE events.task_id = worker_runs.task_id AND events.action = 'log'
      AND json_extract(events.context, '$.audit.action') = 'worker-delegate'
      AND CAST(json_extract(events.context, '$.audit.run_id') AS BIGINT) = worker_runs.id
    ORDER BY events.id DESC LIMIT 1
"""
WORKER_ORDER = f"""
    CASE WHEN {RUNNING} THEN 0
         WHEN worker_runs.status = 'reported' THEN 1
         WHEN worker_runs.status = 'planned' THEN 2
         ELSE 3 END
"""


def is_routine_acknowledgment(note):
    return ROUTINE_ACKNOWLEDGMENT.fullmatch(note.strip()) is not None


def report_activity_sql(acknowledge, *, project_scoped, action_scoped):
    # A linked worker review already has its own report on the original ticket.
    # Prefer the first imported Stamp receipt to any mirrored result.
    project_match = "AND receipt_task.project_prefix = tasks.project_prefix" if project_scoped else ""
    action_match = "AND receipt.action = events.action" if action_scoped else ""
    return f"""
        (events.action IN ('decide', 'assign-human', 'redirect-decision', 'withdraw-decision', 'wait', 'resume')
         OR (events.action = 'done' AND NOT EXISTS (
             SELECT 1 FROM peer_asks WHERE task_id = events.task_id AND requester = events.actor
         ))
         OR (events.action = 'transfer'
             AND (json_extract(events.context, '$.manual_review_result') IS NOT NULL OR EXISTS (
                 SELECT 1 FROM peer_asks AS ask
                 WHERE ask.task_id = events.task_id AND ask.review_run_id IS NULL
                   AND (ask.result_event_id = events.id OR json_extract(events.context, '$.stamp_verdict') IS NOT NULL)
             )))
         OR (events.action IN ('log', 'update') AND TRIM(events.note) <> ''
             AND json_extract(events.context, '$.audit') IS NULL
             AND json_extract(events.context, '$.watcher_measurements') IS NULL
             AND json_extract(events.context, '$.worker_disposition_id') IS NULL
             AND json_extract(events.context, '$.stamp_review') IS NULL
             AND json_extract(events.context, '$.manual_review') IS NULL
             AND NOT {acknowledge}))
        AND events.note <> 'The peer result is ready for acknowledgment.'
        AND events.note NOT LIKE 'Opened peer request %.'
        AND events.note NOT LIKE 'Peer result acknowledged in %. Continue the original work.'
        AND events.note NOT LIKE 'Peer request % was %. Check the next step.'
        AND (json_extract(events.context, '$.stamp_verdict.id') IS NULL OR NOT EXISTS (
            SELECT 1 FROM events AS receipt JOIN tasks AS receipt_task ON receipt_task.id = receipt.task_id
            WHERE json_extract(receipt.context, '$.stamp_verdict.id') = json_extract(events.context, '$.stamp_verdict.id')
              AND COALESCE(json_extract(receipt.context, '$.stamp_verdict.reviewer'), receipt.actor)
                  = COALESCE(json_extract(events.context, '$.stamp_verdict.reviewer'), events.actor)
              AND ((receipt.actor = 'Stamp' AND events.actor <> 'Stamp')
                   OR ((receipt.actor = 'Stamp') = (events.actor = 'Stamp') AND receipt.id < events.id))
              {project_match} {action_match}
        ))
    """


def pacific_day(at):
    return datetime.fromisoformat(at).astimezone(PACIFIC).date().isoformat()


def page(rows, limit, cursor, render):
    visible = rows[:limit]
    return {
        "items": render(visible),
        "next_cursor": visible[-1][cursor] if len(rows) > limit else None,
    }


def matching_excerpt(text, needle):
    folded = text.casefold()
    start = folded.find(needle)
    if start < 0:
        return None
    if len(text) <= ACTIVITY_EXCERPT_LIMIT:
        return " ".join(text.split())

    end = start + len(needle)
    # A folded character such as ß can take more than one position in the match.
    if len(folded) != len(text):
        offsets = [index for index, character in enumerate(text) for _ in character.casefold()]
        start, end = offsets[start], offsets[end - 1] + 1
    width = ACTIVITY_EXCERPT_LIMIT - 2
    padding = max(0, width - (end - start)) // 2
    left = max(0, min(start - padding, len(text) - width))
    right = left + width
    excerpt = " ".join(text[left:right].split())
    return ("…" if left else "") + excerpt + ("…" if right < len(text) else "")


def activity_excerpt(row, needle):
    """Search the projected note and authored Details displayed in Activity."""
    event = watches.display_event(row)
    context = ledger.event_context(event)
    fields = [event["note"], context.get("activity_details")]
    if event["action"] == "decide":
        fields.append(context.get("choice"))
    if event["action"] in ("redirect-decision", "withdraw-decision"):
        fields.append(context.get("decision_reason"))
    for field in fields:
        if isinstance(field, str) and (excerpt := matching_excerpt(field, needle)) is not None:
            return excerpt
    return None


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


def request_log_item(row, people, db, *, recover_title):
    phase = row["state"]
    if row["kind"] == "peer":
        phase = peer_asks.phase(ask=row, task=row, has_started=row["has_started"])
        if phase in ("asked", "in_progress") and row["state"] in ("parked", "waiting"):
            phase = "waiting"
    next_owner = None
    if phase == "acknowledgment":
        next_owner = row["source_owner"] if row["kind"] == "review" else row["requester"]
    elif phase in ("asked", "waiting", "in_progress", "start_unconfirmed"):
        next_owner = row["recipient"]
    request_title = (row["request_title"] or "").strip() or None
    source_title = (row["source_title"] or "").strip() or None
    pin = peer_asks.pending_stamp_review(db, row["id"]) if recover_title and row["kind"] == "peer" and row["is_review"] else None
    pinned_pull = pin["pull"] if pin else row["manual_pull"]
    if recover_title and not (request_title or source_title):
        request_title = peer_asks.request_title(row["request_note"] or "", "", pull=pinned_pull) or None
    item = {
        "id": f"{row['kind']}:{row['id']}",
        "at": row["at"],
        "phase": phase,
        "review": bool(row["is_review"]),
        "requester": row["requester"],
        "recipient": row["recipient"],
        "requester_profile": people.get(row["requester"]),
        "recipient_profile": people.get(row["recipient"]),
        "next_owner": next_owner,
        "next_owner_profile": people.get(next_owner),
        "source_id": row["source_id"],
        "source_key": row["source_key"],
        "source_title": source_title,
        "request_key": row["request_key"],
        "request_title": request_title,
    }
    if recover_title:
        pulls = [pinned_pull] if pinned_pull else sorted(
            peer_asks.review_pull_requests(json.loads(row["request_links"] or "[]"))
        )
        if pulls:
            item["pull_urls"] = [
                f"https://github.com/{repository}/pull/{number}"
                for repository, number in (pull.split("#") for pull in pulls)
            ]
    if row["result_event_id"] is not None:
        item["response"] = {"id": row["result_event_id"], "at": row["response_at"]}
    if row["resolution_id"] is not None and (
        row["kind"] == "review" or row["display_state"] == "closed" or row["current_action"] == "done"
    ):
        item["resolution"] = {"id": row["resolution_id"], "at": row["resolution_at"]}
    return item


class BoardQueries:
    def __init__(self, db, base, *, local_links=False, project_roster=None,
                 standing_exploration=goals.STANDING_EXPLORATION):
        self.db = db
        self.base = base
        self.local_links = local_links
        self.project_roster = project_roster
        self.standing_exploration = standing_exploration

    def schedule_report(self, number, owner, as_of):
        report = recurring.report(self.db, number, owner, as_of)
        report.pop("key")
        profile = profiles.visible_by_keys(self.db, [owner], self.base).get(owner)
        report["owner_name"] = profile["name"] if profile else "Unknown owner"
        return report

    def maintenance_report(self, row, project, as_of):
        report = self.schedule_report(row["id"], row["owner"], as_of)
        choices = [dict(person) for person in self.db.execute(
            """
            SELECT ledger_key AS id, COALESCE(NULLIF(name, ''), NULLIF(username, ''), 'Unknown owner') AS name
            FROM board_principals ORDER BY name COLLATE NOCASE, ledger_key
            """,
        )]
        return {**report, **maintenance.describe(row, project)["schedule"]}, choices

    def schedules(self, project, owner):
        result = {"recurring": [], "maintenance": {"editable": False, "schedule": None, "owner_choices": []}}
        if owner is None:
            return result
        as_of = ledger.timestamp()
        default = projects.default_project(self.db)
        definitions = self.db.execute(
            """
            WITH owned AS (
                SELECT definition.id, COALESCE(definition.project_prefix, local.project, ?) AS project
                FROM recurring_definitions AS definition
                LEFT JOIN local_dris AS local ON local.actor = definition.owner
                WHERE definition.owner = ?
                  AND definition.id NOT IN (SELECT definition_id FROM project_maintenance)
            )
            SELECT id, project FROM owned WHERE (? IS NULL OR project = ?) ORDER BY id
            """, (default, owner, project, project),
        )
        result["recurring"] = [
            {**self.schedule_report(row["id"], owner, as_of), **({"project": row["project"]} if project is None else {})}
            for row in definitions
        ]
        if project is None:
            maintenance_rows = self.db.execute(
                f"""SELECT definition.*, managed.project, managed.members FROM project_maintenance AS managed
                    JOIN recurring_definitions AS definition ON definition.id = managed.definition_id
                    WHERE managed.project IN ({projects.MANAGED_BY}) ORDER BY managed.project""", (owner, owner),
            )
            result["project_maintenance"] = [
                {**self.schedule_report(row["id"], row["owner"], as_of),
                 **maintenance.describe(row, row["project"])["schedule"], "project": row["project"]}
                for row in maintenance_rows
            ]
            return result
        if projects.manages(self.db, project, owner):
            maintenance_view = result["maintenance"]
            maintenance_view["editable"] = True
            row = maintenance.find(self.db, project=project)
            if row is not None:
                maintenance_view["schedule"], maintenance_view["owner_choices"] = self.maintenance_report(
                    row, project, as_of,
                )
        return result

    def visible_schedule(self, number, owner, *, occurrence=False):
        source = "definition.id = ?"
        if occurrence:
            source = "definition.id = (SELECT definition_id FROM recurring_occurrences WHERE task_id = ?)"
        return self.db.execute(
            f"""
            SELECT definition.id, definition.owner, managed.project AS maintenance_project,
                   COALESCE(managed.project, definition.project_prefix, local.project, ?) AS project,
                   CASE WHEN definition.state = 'active' THEN definition.next_at END AS next_at
            FROM recurring_definitions AS definition
            LEFT JOIN project_maintenance AS managed ON managed.definition_id = definition.id
            LEFT JOIN local_dris AS local ON local.actor = definition.owner
            WHERE {source}
              AND ((managed.project IS NULL AND definition.owner = ?)
                   OR managed.project IN ({projects.MANAGED_BY}))
            """, (projects.default_project(self.db), number, owner, owner, owner),
        ).fetchone()

    def schedule_detail(self, number, owner, project=None):
        row = self.visible_schedule(number, owner)
        if row is None or (project is not None and project != row["project"]):
            return None
        as_of = ledger.timestamp()
        if row["maintenance_project"] is not None:
            managed = maintenance.find(self.db, definition_id=number)
            schedule, choices = self.maintenance_report(managed, row["project"], as_of)
            kind = "maintenance"
        else:
            schedule, choices = self.schedule_report(number, row["owner"], as_of), []
            kind = "recurring"
        return {"project": row["project"], "kind": kind, "schedule": schedule, "owner_choices": choices,
                "runs": self.schedule_runs(number)}

    def recurring_runs(self, number, owner):
        """Read a bounded history from the schedule linked to this occurrence."""
        schedule = self.visible_schedule(number, owner, occurrence=True)
        if schedule is None:
            return None
        return {"current_task_id": number, "next_at": schedule["next_at"], "schedule_id": f"R-{schedule['id']}",
                "project": schedule["project"], "items": self.schedule_runs(schedule["id"], current_task_id=number)}

    def schedule_runs(self, number, *, current_task_id=None):
        rows = self.db.execute(
            f"""
            WITH recent AS (
                SELECT due_at, issued_at, task_id
                FROM recurring_occurrences WHERE definition_id = ?
                ORDER BY due_at DESC LIMIT ?
            ),
            history AS (
                SELECT due_at, issued_at, task_id FROM recent
                UNION
                SELECT due_at, issued_at, task_id
                FROM recurring_occurrences WHERE definition_id = ? AND task_id = ?
            )
            SELECT history.due_at, history.issued_at, history.task_id,
                   ticket.id, ticket.project_prefix, ticket.project_number,
                   {ledger.display_state_sql("events.context")} AS display_state,
                   (SELECT at FROM events
                    WHERE events.task_id = history.task_id AND action = 'start'
                    ORDER BY id LIMIT 1) AS started_at,
                   CASE WHEN events.state = 'done' THEN (
                       SELECT at FROM events
                       WHERE events.task_id = history.task_id AND state = 'done'
                         AND (action = 'done' OR json_type(events.context, '$.closure') = 'object')
                       ORDER BY id DESC LIMIT 1
                   ) END AS terminal_at
            FROM history
            LEFT JOIN tasks AS ticket ON ticket.id = history.task_id
            LEFT JOIN events ON events.id = (
                SELECT MAX(id) FROM events
                WHERE task_id = history.task_id AND action NOT IN ('source-event', 'log')
            )
            ORDER BY history.due_at DESC
            """, (number, recurring.HISTORY_LIMIT, number, current_task_id),
        ).fetchall()
        items = []
        for row in rows:
            if row["task_id"] is None:
                state = "missed"
            elif row["display_state"] == "closed":
                state = "closed"
            elif row["display_state"] == "done":
                state = "completed"
            else:
                state = "started" if row["started_at"] is not None else "issued"
            items.append({
                **{field: row[field] for field in ("due_at", "issued_at", "started_at", "terminal_at", "task_id")},
                "state": state,
                "task_key": projects.row_reference(row) if row["id"] is not None else None,
            })
        return items

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

    def owner_progress_event(self, task):
        event_context = "activity.context" if ledger.has_event_context(self.db) else "'{}'"
        return f"""
            SELECT MAX(activity.id) FROM events AS activity
            WHERE activity.task_id = {task}.id AND activity.actor = {task}.dri
              AND ((activity.state = 'in_progress' AND activity.action IN ('start', 'update', 'resume'))
                   OR (activity.state IN ('in_progress', 'waiting') AND activity.action = 'log'
                       AND json_extract({event_context}, '$.watcher_measurements') IS NULL
                       AND ({event_context} = '{{}}'
                            OR json_extract({event_context}, '$.activity_details') IS NOT NULL)))
        """

    def owner_progress(self, task_ids):
        if not task_ids:
            return {}
        marks = ", ".join("?" for _ in task_ids)
        return {
            report["task_id"]: {"id": report["id"], "at": report["at"]}
            for report in self.db.execute(
                f"""
                SELECT tasks.id AS task_id, events.id, events.at FROM tasks
                JOIN events ON events.id = ({self.owner_progress_event('tasks')})
                WHERE tasks.id IN ({marks})
                """, tuple(sorted(task_ids)),
            )
        }

    def task_rows(self, rows, *, include_report_fields=False, include_release_results=False):
        if not rows:
            return []
        ids = [row["id"] for row in rows]
        placeholders = ", ".join("?" for _ in ids)
        requests = peer_asks.source_links(self.db, ids) if watches.has_table(self.db, "peer_asks") else {}
        visible_steps = "AND removed = 0" if "removed" in tasks_storage.table_columns(self.db, "task_steps") else ""
        progress = {
            row["task_id"]: (row["completed"], row["total"])
            for row in self.db.execute(
                f"""
                SELECT task_id, SUM(checked) AS completed, COUNT(*) AS total
                FROM task_steps WHERE task_id IN ({placeholders}) {visible_steps} GROUP BY task_id
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
        owner_keys = [row["dri"] for row in rows]
        if include_report_fields:
            owner_keys.extend(ask["requester"] for ask in requests.values())
        owners = profiles.visible_by_keys(self.db, owner_keys, self.base)
        reporting_tasks = {
            row["id"] for row in rows
            if include_report_fields and row["state"] in ("in_progress", "waiting")
            and (owners.get(row["dri"], {}).get("kind") == "human"
                 or owners.get(row["dri"], {}).get("dri_kind") == "local")
        }
        work_reports = self.owner_progress(reporting_tasks)
        recipients = profiles.visible_by_keys(self.db, [row["waiting_on"] for row in rows if row["waiting_on"]], self.base)
        tasks_by_id = {row["id"]: row for row in rows}
        wait_starts = self.wait_starts(rows)
        attention_waits = set()
        attention_events = [row["event_id"] for row in rows
                            if row["state"] == "waiting" and row["waiting_on"] == "Needs me"]
        if attention_events and watches.has_table(self.db, "attention_wait_links"):
            marks = ", ".join("?" for _ in attention_events)
            attention_waits = {row[0] for row in self.db.execute(
                f"SELECT event_id FROM attention_wait_links WHERE event_id IN ({marks})", attention_events,
            )}

        def lookup(number):
            if number not in tasks_by_id:
                tasks_by_id[number] = ledger.find_task(self.db, number)
            return tasks_by_id[number]

        release_ids = [row["id"] for row in rows if projects.release_queue_enabled(projects.row_project(row))]
        release_fields = releases.ticket_fields(self.db, release_ids)
        release_results = releases.ticket_results(self.db, release_ids) if include_release_results else {}
        goal_rows = {}
        if tasks_storage.table_columns(self.db, "task_goals"):
            goal_rows = {row["task_id"]: row for row in self.db.execute(
                f"""
                SELECT task_id, kind, deadline, ticket_ids FROM task_goals
                WHERE retired = 0 AND task_id IN ({placeholders})
                """, ids,
            )}
        results = []
        local_watchers = {}
        for row in rows:
            label, note = ledger.current_summary(row)
            goal = goal_rows.get(row["id"])
            if goal is not None and row["state"] == "parked":
                guidance = goals.parked_guidance(goal, note, self.standing_exploration)
                if guidance is not None:
                    label, note = guidance
            complete, total = progress.get(row["id"], (0, 0))
            context = ledger.event_context(row)
            details = context.get("activity_details")
            blockers = ledger.blocker_chain(self.db, row["id"], ledger.ticket_blocker(row), lookup)
            wait_kind = None
            if row["state"] == "human_review":
                wait_kind = "person"
            elif row["state"] == "waiting":
                if blockers:
                    wait_kind = "ticket"
                elif context.get("wait_kind") == "automation":
                    wait_kind = "automation"
                elif row["event_id"] in attention_waits:
                    wait_kind = "attention"
                elif context.get("peer_ask_ticket") and row["waiting_on"] in recipients:
                    wait_kind = "person"
                else:
                    wait_kind = "legacy"
            item = {
                "id": row["id"],
                "key": projects.row_reference(row),
                "title": row["title"],
                **({"record_kind": "knowledge"} if ledger.is_knowledge(row) else {}),
                "dri": row["dri"],
                "priority": row["priority"],
                "dri_profile": owners.get(row["dri"]),
                "state": row["state"],
                **({"goal_kind": goal["kind"]} if goal is not None else {}),
                **({"display_state": "closed"} if row["display_state"] == "closed" else {}),
                "note": note,
                "summary_label": label,
                "waiting_on": row["waiting_on"],
                "waiting_profile": recipients.get(row["waiting_on"]),
                **({"wait_kind": wait_kind} if wait_kind else {}),
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
            if ask := requests.get(row["id"]):
                item["peer_request"] = {
                    "source_id": ask["source_task_id"], "source_key": projects.row_reference(ask),
                    "phase": peer_asks.phase(ask, row, has_started=ask["has_started"]), "requester": ask["requester"],
                    "asked_at": ask["asked_at"],
                }
                if include_report_fields and (requester := owners.get(ask["requester"])):
                    item["peer_request"]["requester_name"] = requester.get("name") or requester.get("username")
            if include_report_fields:
                item["last_activity_at"] = activity_times.get(row["id"], row["at"])
                if row["id"] in reporting_tasks:
                    item["reported_progress"] = work_reports.get(row["id"])
                    if row["state"] == "in_progress" and owners.get(row["dri"], {}).get("dri_kind") == "local":
                        watcher = row["dri"], projects.row_project(row)
                        if watcher not in local_watchers:
                            local_watchers[watcher] = local_dri.ready(self.db, *watcher)
                        item["local_watcher"] = "ready_only" if local_watchers[watcher] else "off"
                if isinstance(details, str):
                    item["details"] = details
            if receipt := release_results.get(row["id"]):
                item["release_result"] = receipt
                item["release_after_step"] = datetime.fromisoformat(receipt["at"]) > datetime.fromisoformat(row["at"])
            results.append(item)
        return results

    def hide_peer_requests(self, hide_requests):
        return hide_requests and watches.has_table(self.db, "peer_asks")

    def tasks(self, *, state=None, dri=None, waiting_on=None, before=None, limit=20, now=False,
              decided_by=None, search=None, priority_order=False, review_due=None, project=None, tag=None,
              managed_by=None, hide_requests=False, exclude_goals=False, github_url=None, include_knowledge=False):
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
        if self.hide_peer_requests(hide_requests):
            terms.append("NOT EXISTS (SELECT 1 FROM peer_asks WHERE peer_asks.task_id = tasks.id)")
        if exclude_goals:
            terms.append("NOT EXISTS (SELECT 1 FROM task_goals WHERE task_goals.task_id = tasks.id AND retired = 0)")
        if managed_by is not None:
            terms.append(f"tasks.project_prefix IN ({projects.MANAGED_BY})")
            args.extend((managed_by, managed_by))
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
        if github_url is not None:
            terms.append(GITHUB_SOURCE_MATCH)
            args.extend((github_url, github_url + "/"))
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
            sql = ledger.current_tasks_sql(self.db, include_knowledge=include_knowledge) + where + f" ORDER BY {order} LIMIT ?"
            rows = self.db.execute(sql, (*args, limit + 1)).fetchall()
        else:
            rows = self.search_rows(terms, args, order, limit, priority_order, filter_search,
                                    include_knowledge=include_knowledge)
        include_report_fields = dri is not None and state in WORK_STATES
        result = page(rows, limit, "event_id", lambda visible: self.task_rows(
            visible, include_report_fields=include_report_fields,
        ))
        if priority_order and result["next_cursor"] is not None:
            last = rows[limit - 1]
            result["next_cursor"] = f"{last['priority']}:{last['event_id']}"
        return result

    def maintained_decision_rows(self, *, search=None, entry=None, source_ticket=None, before=None, limit,
                                 github_url=None, project=None, maintenance_dri=None, include_activity=False, updated_order=False):
        context = "events.context" if ledger.has_event_context(self.db) else "'{}'"
        legacy = f"""({ledger.display_state_sql(context)} = 'done'
            AND tasks.id IN (SELECT task_id FROM task_tags WHERE tag = ?))"""
        typed = "tasks.record_kind = 'knowledge' OR " if "record_kind" in ledger.task_columns(self.db) else ""
        terms = [f"({typed}{legacy})"]
        args = [DECISION_RECORD_TAG]
        if project is not None:
            terms.append("tasks.project_prefix = ?")
            args.append(project)
        if maintenance_dri is not None:
            terms.append("tasks.id IN (SELECT task_id FROM knowledge_maintenance_assignments WHERE dri = ?)")
            args.append(maintenance_dri)
        if before is not None:
            if updated_order:
                cursor = self.db.execute(f"SELECT {ledger.event_time_sql('at')} FROM events WHERE id = ?", (before,)).fetchone()
                if cursor is None:
                    return []
                terms.append(f"({ledger.event_time_sql('latest_activity.at')}, latest_activity.id) < (?, ?)")
                args.extend((cursor[0], before))
            else:
                terms.append("events.id < ?")
                args.append(before)
        if entry is not None:
            terms.append("tasks.id = ?")
            args.append(entry)
        if source_ticket is not None:
            if not watches.has_table(self.db, "task_references"):
                return []
            terms.append("""
                (EXISTS (
                    SELECT 1 FROM task_references AS reference
                    JOIN events AS source_event ON reference.url = ? || source_event.id || '-log'
                    WHERE reference.task_id = tasks.id AND source_event.task_id = ?
                ) OR EXISTS (
                    SELECT 1 FROM knowledge_stages AS stage
                    WHERE stage.entry_id = tasks.id AND stage.source_id = ?
                ))
            """)
            args.extend((f"{self.base}/#event-", source_ticket, source_ticket))
        if github_url is not None:
            terms.append(GITHUB_SOURCE_MATCH)
            args.extend((github_url, github_url + "/"))
        order = "activity_sort_at DESC, activity_id DESC" if updated_order else "events.id DESC"
        base = ledger.current_tasks_sql(self.db, include_knowledge=True, include_activity=include_activity)
        if search:
            needle = search.casefold()
            area_matches = {row["task_id"] for row in self.db.execute("SELECT task_id, path FROM knowledge_entries")
                            if needle in row["path"].casefold()}

            def matches(row):
                return (needle == projects.row_reference(row).casefold()
                        or needle in row["title"].casefold()
                        or needle in (row["brief"] or "").casefold()
                        or row["id"] in area_matches)

            return self.search_rows(terms, args, order, limit, False, matches,
                                    base_sql=base, updated_order=updated_order)
        where = " WHERE " + " AND ".join(terms)
        return self.db.execute(
            base + where + f" ORDER BY {order} LIMIT ?",
            (*args, limit + 1),
        ).fetchall()

    def maintained_decisions(self, *, actor, search=None, entry=None, source_ticket=None, before=None, limit,
                             project=None, maintenance_dri=None, source_project=None, updated_order=False):
        def render(rows):
            assignments = knowledge.maintenance_assignments(self.db, [row["id"] for row in rows])
            managed = {row["prefix"] for row in self.db.execute(projects.MANAGED_BY, (actor, actor))}
            return [{
                "id": row["id"], "key": projects.row_reference(row), "title": row["title"],
                "project": projects.row_project(row), "maintenance_dri": assignments.get(row["id"]),
                "can_assign_maintenance_dri": projects.row_project(row) in managed,
                "can_reconcile": projects.row_project(row) in managed or (not ledger.is_knowledge(row) and row["dri"] == actor),
                "last_activity": {
                    "at": row["activity_at"], "id": row["activity_id"],
                    "label": ledger.event_label(row["activity_action"], json.loads(row["activity_context"])),
                } if row["activity_at"] else None,
                "summary": row["brief"], "sources": self.decision_sources(row["id"], project=source_project),
                **knowledge.metadata(self.db, row["id"], source_project=source_project),
            } for row in rows]

        with tasks_storage.read(self.db):
            rows = self.maintained_decision_rows(
                search=search, entry=entry, source_ticket=source_ticket, before=before, limit=limit,
                project=project, maintenance_dri=maintenance_dri, include_activity=True, updated_order=updated_order,
            )
            return page(rows, limit, "activity_id" if updated_order else "event_id", render)

    def search_with_decisions(self, *, before, limit, search=None, github_url=None, project=None):
        """Search ordinary T tickets and maintained decisions on the verified board."""
        filters = {"search": search, "github_url": github_url, "before": before}
        with tasks_storage.read(self.db):
            tickets = self.tasks(project=project or projects.DEFAULT, limit=limit + 1, **filters)["items"]
            records = self.maintained_decision_rows(limit=limit, project=project, **filters)
            mixed = {task["id"]: {
                "id": task["id"], "event_id": task["event_id"], "key": task["key"], "title": task["title"],
                "state": task.get("display_state", task["state"]),
            } for task in tickets}
            # Preserve the extra candidate row when an ordinary T ticket is also a decision.
            mixed.update({
                row["id"]: {
                    "id": row["id"], "event_id": row["event_id"], "key": projects.row_reference(row),
                    "title": row["title"], "state": row["display_state"],
                    **({"record_kind": "knowledge"} if ledger.is_knowledge(row) else {}),
                }
                for row in records
            })
            ordered = sorted(mixed.values(), key=lambda row: row["event_id"], reverse=True)
            decisions = {row["id"]: row for row in records}

            def render(rows):
                result = []
                for row in rows:
                    item = {key: row[key] for key in ("id", "key", "title", "state")}
                    if ledger.is_knowledge(row):
                        item["record_kind"] = "knowledge"
                    if row["id"] in decisions:
                        item["decision"] = {
                            "summary": decisions[row["id"]]["brief"],
                            "sources": self.decision_sources(row["id"]),
                            **knowledge.metadata(self.db, row["id"]),
                        }
                    result.append(item)
                return result

            return page(ordered, limit, "event_id", render)

    def decision_sources(self, task_id, *, project=None):
        prefix = f"{self.base}/#event-"
        sources = []
        for _, label, url, _ in watches.task_source_links(self.db, task_id):
            if match := re.fullmatch(rf"{re.escape(prefix)}([1-9][0-9]{{0,18}})-log", url):
                event_id = int(match[1])
                if event_id > projects.MAX_ID:
                    continue
                event = self.db.execute(
                    "SELECT events.id, events.at, events.task_id, tasks.title AS task_title, tasks.project_prefix, "
                    + TASK_REFERENCE + " AS task_key FROM events JOIN tasks ON tasks.id = events.task_id "
                    "WHERE events.id = ?", (event_id,),
                ).fetchone()
                if event is not None and project in (None, event["project_prefix"]):
                    sources.append({
                        "label": label, "url": url, "unavailable": False,
                        "event_id": event["id"], "at": event["at"], "task_id": event["task_id"],
                        "task_key": event["task_key"], "task_title": event["task_title"],
                    })
        return sources

    def task_suggestions(self, search, *, project):
        needle = search.casefold()
        key = SUGGESTION_KEY.fullmatch(search.upper())
        if key and project not in (None, key[1]):
            return []
        project = key[1] if key else project
        prefix = key[1] + "-" + (key[2].lstrip("0") or key[2]) if key else None
        words = SEARCH_WORDS.findall(needle)
        scope = " WHERE project_prefix = ?" if project else ""
        params = (project,) if project else ()
        query = ("SELECT id, title, project_prefix, project_number, event_id FROM ("
                 + ledger.current_tasks_sql(self.db) + ") AS current")

        def matches():
            for row in self.db.execute(query + scope, params):
                reference = projects.row_reference(row)
                if prefix is not None:
                    rank = 0 if reference == prefix else 1 if reference.startswith(prefix) else None
                else:
                    rank = title_suggestion_rank(needle, words, row["title"].casefold())
                if rank is not None:
                    yield rank, -row["event_id"], {"id": row["id"], "key": reference, "title": row["title"]}

        return [row for _, _, row in nsmallest(6, matches(), key=lambda match: match[:2])]

    def search_rows(self, terms, args, order, limit, priority_order, matches, *,
                    include_knowledge=False, base_sql=None, updated_order=False):
        rows = []
        cursor, bindings = [], ()
        batch_size = max(100, limit + 1)
        snapshot = nullcontext() if self.db.in_transaction else tasks_storage.read(self.db)
        with snapshot:
            base = base_sql or ledger.current_tasks_sql(self.db, include_knowledge=include_knowledge)
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
                elif updated_order:
                    cursor = [f"({ledger.event_time_sql('latest_activity.at')}, latest_activity.id) < (?, ?)"]
                    bindings = (last["activity_sort_at"], last["activity_id"])
                else:
                    cursor, bindings = ["events.id < ?"], (last["event_id"],)
        return rows[:limit + 1]

    def overview(self, *, now_before, decisions_before, limit, recipient, project=projects.DEFAULT):
        project_term, bindings = ("tasks.project_prefix = ?", (project,)) if project is not None else ("1=1", ())
        counts = dict.fromkeys(ledger.STATES, 0)
        counts.update(self.db.execute(
            "SELECT display_state, COUNT(*) FROM (" + ledger.current_tasks_sql(self.db) +
            f" WHERE {project_term}) GROUP BY display_state", bindings,
        ))
        owners = [row[0] for row in self.db.execute(
            f"SELECT DISTINCT dri FROM tasks WHERE {project_term} AND dri <> '' "
            f"AND {ledger.work_records_sql(self.db)} ORDER BY dri LIMIT 256", bindings,
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

    def detail(self, number, *, project_extensions=None, owner=None):
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
        related, source_links = ledger.task_links(self.db, row, steps, self.base + "/")
        sources = []
        github = {}
        extensions = (project_extensions or {}).get(projects.row_project(row), {})
        if extensions.get("github-status"):
            from tasks_extensions import source_states

            github = source_states(self.db, number)
        for label, url, generated in source_links:
            link, = self.links([(label, url)])
            if generated:
                link["generated"] = True
            if url in github:
                link["github"] = github[url]
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
        task = {**self.task_rows([row])[0], "record_kind": row["record_kind"], "eta": row["eta"]}
        if owner and owner == task["dri"] and (peer := task.get("peer_request")):
            peer["review_run_id"] = peer_asks.find(self.db, number)["review_run_id"]
        result = {
            "task": task,
            "brief": row["brief"],
            "checklist": steps,
            "related_tasks": related,
            "sources": sources,
            "goal": goals.ticket_details(self.db, row),
            "workers": runs,
            "last_activity": self.last_activity(task_id=number),
        }
        if owner is not None and (history := self.recurring_runs(number, owner)) is not None:
            result["recurring_runs"] = history
        return result

    def last_activity(self, *, owner=None, task_id=None, project=None):
        column, value = ("tasks.dri", owner) if owner is not None else ("events.task_id", task_id)
        scope = ledger.work_records_sql(self.db) if owner is not None else "TRUE"
        event = self.db.execute(
            f"""
            SELECT events.id, events.at
            FROM tasks JOIN events ON events.task_id = tasks.id
            WHERE {column} = ? AND (? IS NULL OR tasks.project_prefix = ?) AND {scope}
            ORDER BY events.id DESC LIMIT 1
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
        visible = [watches.display_event(row) for row in rows]
        contexts = [ledger.event_context(row) for row in visible]
        owner_keys = ("previous_dri", "next_dri")
        keys.extend(context[key] for context in contexts for key in owner_keys
                    if isinstance(context.get(key), str) and context[key])
        people = profiles.visible_by_keys(self.db, keys, self.base)
        events = []
        for stored, row, context in zip(rows, visible, contexts, strict=True):
            details = context.pop("activity_details", None)
            if row["id"] in work_kinds:
                context["work_kind"] = work_kinds[row["id"]]
            actor_profile = people.get(row["actor"]) if row["actor"] == stored["actor"] else None
            links = self.links(json.loads(row["links"]), event_id=row["id"])
            event = {
                **{key: row[key] for key in (
                    "id", "task_id", "task_key", "task_title", "at", "actor", "action", "state", "note", "waiting_on",
                )},
                "label": ledger.event_label(row["action"], context),
                "context": context,
                "links": links,
                "actor_profile": actor_profile,
                "waiting_profile": people.get(row["waiting_on"]),
                **{f"{key}_profile": people.get(context.get(key)) for key in owner_keys if key in context},
                "related_tasks": ledger.resolve_task_references(
                    self.db, row["task_id"], (row["note"], details, *(link["url"] for link in links)),
                    self.base + "/",
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
               dri=None, feed=None, day=None, project=None, time_zone=PACIFIC, hide_requests=False, work_only=False):
        joins, terms, args = "", [], []
        if work_only or dri is not None:
            terms.append(ledger.work_records_sql(self.db))
        if self.hide_peer_requests(hide_requests):
            terms.append("NOT EXISTS (SELECT 1 FROM peer_asks WHERE peer_asks.task_id = tasks.id)")
        if watcher is not None:
            joins = " JOIN task_watches ON task_watches.task_id = events.task_id"
            terms.extend(("task_watches.watcher = ?", "events.id > task_watches.after_event"))
            args.append(watcher)
        if owner_or_watcher is not None:
            terms.append(
                f"""((tasks.dri = ? AND {ledger.work_records_sql(self.db)}) OR EXISTS (
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
        if actor is not None and feed == "reports":
            terms.append(
                "(events.actor = ? OR (events.actor = 'Stamp' AND events.action = 'log' "
                "AND json_extract(events.context, '$.stamp_verdict.reviewer') = ?))"
            )
            args.extend((actor, actor))
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
        elif feed in ("outcomes", "reports"):
            if isinstance(self.db, tasks_storage.Postgres):
                acknowledge = r"(regexp_replace(events.note, '^\s+|\s+$', '', 'g') ~* ?)"
                args.append("^" + ROUTINE_ACKNOWLEDGMENT.pattern + "$")
            else:
                self.db.create_function("routine_acknowledgment", 1, is_routine_acknowledgment, deterministic=True)
                acknowledge = "routine_acknowledgment(events.note)"
            if feed == "reports":
                terms.append(report_activity_sql(
                    acknowledge, project_scoped=project is not None, action_scoped=action is not None,
                ))
            else:
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

    def search_activity(self, search, *, project, before, limit):
        needle = search.casefold()
        # Page by the event primary key before joining so sparse matches do not
        # make the database sort an entire project's history for every batch.
        sql = f"""
            WITH batch AS (
                SELECT id, task_id FROM events WHERE id <= ? ORDER BY id DESC LIMIT ?
            )
            SELECT batch.id AS scan_id, events.id, events.actor, events.action, events.note,
                   events.links, events.context, {TASK_REFERENCE} AS task_key
            FROM batch
            LEFT JOIN tasks ON tasks.id = batch.task_id AND tasks.project_prefix = ?
            LEFT JOIN events ON events.id = batch.id AND tasks.id IS NOT NULL
            ORDER BY batch.id DESC
        """
        cursor = projects.MAX_ID if before is None else before - 1
        matches = []
        batch_size = 100
        with tasks_storage.read(self.db):
            while len(matches) <= limit:
                rows = self.db.execute(sql, (cursor, batch_size, project)).fetchall()
                for row in rows:
                    if row["id"] is None:
                        continue
                    excerpt = activity_excerpt(row, needle)
                    if excerpt is not None:
                        matches.append({
                            "id": row["id"], "task_key": row["task_key"], "excerpt": excerpt,
                            "url": f"{self.base}/#event-{row['id']}-log",
                        })
                        if len(matches) > limit:
                            break
                if len(rows) < batch_size:
                    break
                cursor = rows[-1]["scan_id"] - 1
        return page(matches, limit, "id", list)

    def outcome_days(self, *, dri, before, limit, project=None, time_zone=PACIFIC, week_ending=None):
        day_sql = self.outcome_day_sql(time_zone)
        terms, args = [OUTCOME_ACTIONS], []
        if dri is not None:
            terms.extend(("tasks.dri = ?", ledger.work_records_sql(self.db)))
            args.append(dri)
        if project is not None:
            terms.append("tasks.project_prefix = ?")
            args.append(project)
        week = {}
        if week_ending is not None:
            calendar = [(week_ending - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]
            counts = dict(self.db.execute(
                f"SELECT {day_sql} AS day, COUNT(*) "
                "FROM events JOIN tasks ON tasks.id = events.task_id "
                f"WHERE {' AND '.join(terms)} AND {day_sql} BETWEEN ? AND ? GROUP BY day",
                (*args, calendar[0], calendar[-1]),
            ).fetchall())
            week = {"week": [{"day": day, "count": counts.get(day, 0)} for day in calendar]}
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
        return {"days": days[:limit], "next_day": days[limit - 1] if len(days) > limit else None, **week}

    def related_activity(self, person, *, before, limit, project=None):
        result = self.events(before=before, limit=limit, owner_or_watcher=person, project=project)
        failures = self.db.execute(
            f"""
            SELECT sources.source_key, intake_errors.error, intake_errors.at
            FROM intake_errors JOIN sources ON sources.id = intake_errors.source_id
            WHERE EXISTS (
                SELECT 1 FROM task_sources
                JOIN tasks ON tasks.id = task_sources.task_id
                LEFT JOIN task_watches ON task_watches.task_id = tasks.id AND task_watches.watcher = ?
                WHERE task_sources.source_id = sources.id AND (? IS NULL OR tasks.project_prefix = ?)
                    AND (task_watches.watcher IS NOT NULL OR (tasks.dri = ? AND {ledger.work_records_sql(self.db)}))
            )
            ORDER BY intake_errors.at DESC, sources.source_key LIMIT 20
            """, (person, project, project, person),
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
            WHERE tasks.dri = ? AND (? IS NULL OR tasks.project_prefix = ?)
                AND {ledger.work_records_sql(self.db)}{where}
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

    def actionable_reviews(self, person, *, before, limit, project=None, unlinked=False):
        unlinked_filter = ""
        if unlinked and watches.has_table(self.db, "peer_asks"):
            unlinked_filter = "AND NOT EXISTS (SELECT 1 FROM peer_asks WHERE review_run_id = worker_runs.id)"
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
                AND (? IS NULL OR tasks.project_prefix = ?) {unlinked_filter}
            ORDER BY worker_runs.id DESC LIMIT ?
            """, (person, before or projects.MAX_ID, project, project, limit + 1),
        ).fetchall()
        return page(rows, limit, "id", lambda visible: [dict(row) for row in visible])

    def wake_workers(self, person, status, *, before, limit):
        """Return minimal wake routing for the signed worker, using current task state."""
        mode = "json_extract(wake.context, '$.audit.mode')"
        check_at = "json_extract(wake.context, '$.audit.at')"
        assigned = "worker_runs.worker = ?"
        if status == "planned":
            eligible = f"{assigned} AND worker_runs.status = 'planned' AND worker_runs.role = 'review'"
            params = (person,)
        elif status == "running":
            eligible = (f"{assigned} AND ({RUNNING}) AND current.display_state = 'in_progress' "
                        f"AND COALESCE({mode}, 'clear') = 'clear'")
            params = (person,)
        else:
            eligible = f"""
                ({assigned} AND ({RUNNING}) AND {mode} = 'at' AND {check_at} <= ?)
                OR (current.dri = ? AND worker_runs.status = 'reported' AND {mode} = 'report')
            """
            params = (person, ledger.timestamp(), person)
        unlinked = ""
        if watches.has_table(self.db, "peer_asks"):
            unlinked = "AND NOT EXISTS (SELECT 1 FROM peer_asks WHERE review_run_id = worker_runs.id)"
        rows = self.db.execute(
            f"""
            WITH current AS ({ledger.current_tasks_sql(self.db)})
            SELECT worker_runs.id, tasks.id AS task_id, {TASK_REFERENCE} AS task_key,
                   CASE WHEN current.dri = ? THEN 1 ELSE 0 END AS coordinator,
                   wake.id AS check_event, {mode} AS check_mode
            FROM worker_runs JOIN tasks ON tasks.id = worker_runs.task_id
            JOIN current ON current.id = tasks.id
            LEFT JOIN events AS wake ON wake.id = ({workers.LATEST_WAKE_CHECK})
            WHERE current.display_state IN ('ready', 'in_progress') AND worker_runs.id < ?
              AND ({eligible}) {unlinked}
            ORDER BY worker_runs.id DESC LIMIT ?
            """, (person, before or projects.MAX_ID, *params, limit + 1),
        ).fetchall()
        return page(rows, limit, "id", lambda visible: [dict(row) for row in visible])

    def queued_reviews(self, *, before, limit, project=projects.DEFAULT):
        requester = f"({REVIEW_REQUESTER})" if ledger.has_event_context(self.db) else "NULL"
        rows = self.db.execute(
            f"""
            WITH current AS ({ledger.current_tasks_sql(self.db)})
            SELECT worker_runs.id, worker_runs.task_id, worker_runs.worker, worker_runs.status,
                   worker_runs.assigned_at AS at, {requester} AS requester,
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
            people = profiles.visible_by_keys(
                self.db, [key for row in visible for key in (row["worker"], row["requester"]) if key], self.base,
            )
            linked = peer_asks.review_links(self.db, [row["id"] for row in visible]) if watches.has_table(self.db, "peer_asks") else {}
            return [{
                "id": row["id"], "task_id": row["task_id"], "task_title": row["task_title"],
                "task_key": row["task_key"],
                "recipient": people.get(row["worker"], {}).get("name") or row["worker"],
                "requester": people.get(row["requester"], {}).get("name") or row["requester"],
                "at": row["at"], "state": row["status"],
                "execution_state": row["execution_state"],
                **({"linked_request": linked[row["id"]]} if row["id"] in linked else {}),
            } for row in visible]

        return page(rows, limit, "id", render)

    _PEER_WAIT_JOIN = """
        JOIN board_principals AS peer ON peer.ledger_key = COALESCE(
            (SELECT ledger_key FROM board_principals WHERE ledger_key = tasks.waiting_on),
            (SELECT MIN(ledger_key) FROM board_principals
             WHERE username <> '' AND LOWER(username) = LOWER(tasks.waiting_on)
             HAVING COUNT(*) = 1)
        )
    """
    _PEER_WAIT_CONDITIONS = """
        tasks.state = 'waiting' AND peer.kind = 'agent' AND peer.ledger_key <> tasks.dri
        AND COALESCE(json_extract(tasks.context, '$.wait_kind'), '') <> 'automation'
        AND json_extract(tasks.context, '$.blocked_by') IS NULL
        AND (json_extract(tasks.context, '$.named_peer_wait') IS NOT NULL OR NOT EXISTS (
            SELECT 1 FROM worker_runs
            WHERE task_id = tasks.id AND worker = peer.ledger_key AND role = 'review'
              AND status IN ('planned', 'running')
        ))
    """

    def queued_peer_waits(self, *, before, limit, project=projects.DEFAULT, legacy_recipient=None):
        legacy = "" if legacy_recipient is None else (
            "AND peer.ledger_key = ? AND json_extract(tasks.context, '$.peer_ask_ticket') IS NULL"
        )
        args = () if legacy_recipient is None else (legacy_recipient,)
        rows = self.db.execute(
            f"""
            WITH current AS ({ledger.current_tasks_sql(self.db)} WHERE tasks.project_prefix = ?)
            SELECT tasks.event_id AS id, tasks.id AS task_id, tasks.title AS task_title,
                   {TASK_REFERENCE} AS task_key, tasks.at, request_event.actor AS requester,
                   json_extract(tasks.context, '$.peer_ask_ticket') AS linked_request,
                   (SELECT task_id FROM peer_asks WHERE source_wait_event_id = tasks.event_id
                    AND json_extract(tasks.context, '$.named_peer_wait') IS NOT NULL) AS linked_request_id,
                   COALESCE(NULLIF(peer.name, ''), NULLIF(peer.username, ''), peer.ledger_key) AS recipient
            FROM current AS tasks
            JOIN events AS request_event ON request_event.id = tasks.event_id
            {self._PEER_WAIT_JOIN}
            WHERE {self._PEER_WAIT_CONDITIONS} AND tasks.event_id < ? {legacy}
            ORDER BY tasks.event_id DESC LIMIT ?
            """, (project, before or projects.MAX_ID, *args, limit + 1),
        ).fetchall()
        def render(visible):
            people = profiles.visible_by_keys(self.db, [row["requester"] for row in visible], self.base)
            return [{
                **{key: row[key] for key in row.keys() if key not in ("linked_request", "linked_request_id") or row[key]},
                "requester": people.get(row["requester"], {}).get("name") or row["requester"],
            } for row in visible]

        return page(rows, limit, "id", render)

    def recent_peer_responses(self, project):
        if not watches.has_table(self.db, "peer_asks"):
            return {"items": []}
        rows = self.db.execute(
            f"""
            SELECT response.id, response.at, response.actor, peer_asks.requester,
                   tasks.id AS task_id, {TASK_REFERENCE} AS task_key, source.title AS task_title
            FROM peer_asks
            JOIN events AS response ON response.id = peer_asks.result_event_id
            JOIN tasks ON tasks.id = peer_asks.task_id
            JOIN tasks AS source ON source.id = peer_asks.source_task_id
            WHERE tasks.project_prefix = ?
            ORDER BY response.at DESC, response.id DESC LIMIT 5
            """, (project,),
        ).fetchall()
        people = profiles.visible_by_keys(
            self.db, [key for row in rows for key in (row["actor"], row["requester"])], self.base,
        )
        return {"items": [{
            "id": row["id"], "at": row["at"], "task_id": row["task_id"],
            "task_key": row["task_key"], "task_title": row["task_title"],
            "recipient": people.get(row["requester"], {}).get("name") or row["requester"],
            "responder": people.get(row["actor"], {}).get("name") or row["actor"],
        } for row in rows]}

    def peer_request_history(self, *, project, before=None, limit=20):
        if not watches.has_table(self.db, "peer_asks"):
            return {"items": [], "next_cursor": None}
        rows = self.db.execute(
            f"""
            WITH requests AS ({ledger.current_tasks_sql(self.db)})
            SELECT requests.id, requests.state, requests.display_state,
                   requests.event_id AS latest_id, requests.current_action, requests.at AS latest_at, requests.context,
                   ask.requester, ask.recipient, ask.result_event_id,
                   source.id AS source_id, source.title AS source_title,
                   source.project_prefix || '-' || COALESCE(source.project_number, source.id) AS source_key,
                   requests.project_prefix || '-' || COALESCE(requests.project_number, requests.id) AS task_key,
                   response.at AS response_at,
                   (SELECT event.at FROM events AS event
                    WHERE event.task_id = requests.id AND event.action = 'add'
                    ORDER BY event.id LIMIT 1) AS asked_at,
                   EXISTS (SELECT 1 FROM events AS event
                           WHERE event.task_id = requests.id AND event.action = 'start') AS has_started
            FROM peer_asks AS ask
            JOIN requests ON requests.id = ask.task_id
            JOIN tasks AS source ON source.id = ask.source_task_id
            LEFT JOIN events AS response ON response.id = ask.result_event_id AND response.task_id = requests.id
            WHERE requests.project_prefix = ? AND source.project_prefix = ? AND requests.id < ?
            ORDER BY requests.id DESC LIMIT ?
            """, (project, project, before or projects.MAX_ID, limit + 1),
        ).fetchall()

        def render(visible):
            participants = {row[field] for row in visible for field in ("requester", "recipient")}
            people = profiles.visible_by_keys(self.db, participants, self.base)
            items = []
            for row in visible:
                state = peer_asks.phase(ask=row, task=row, has_started=row["has_started"])
                item = {
                    "id": row["id"], "task_key": row["task_key"], "phase": state,
                    "requester": people.get(row["requester"], {}).get("name") or row["requester"],
                    "recipient": people.get(row["recipient"], {}).get("name") or row["recipient"],
                    "source_id": row["source_id"], "source_key": row["source_key"],
                    "source_title": row["source_title"], "asked_at": row["asked_at"],
                }
                if row["response_at"] is not None:
                    item["response"] = {"id": row["result_event_id"], "at": row["response_at"]}
                if row["display_state"] == "closed" or (state == "done" and row["current_action"] == "done"):
                    item["resolution"] = {"id": row["latest_id"], "at": row["latest_at"]}
                items.append(item)
            return items

        return page(rows, limit, "id", render)

    def request_log(self, *, project, before=None, limit=20, actionable_reviewer=None, recover_title=False):
        project_filter = "WHERE tasks.project_prefix = ?" if project is not None else ""
        filters, args = [], [project] if project is not None else []
        if actionable_reviewer is not None:
            filters.append("""
                recipient = ? AND is_review AND (
                    (kind = 'peer' AND state IN ('ready', 'in_progress') AND result_event_id IS NULL)
                    OR (kind = 'review' AND state IN ('asked', 'in_progress', 'start_unconfirmed'))
                )
            """)
            args.append(actionable_reviewer)
        if before:
            filters.append("(at, kind, id) < (?, ?, ?)")
            args.extend(before)
        where = "WHERE " + " AND ".join(filters) if filters else ""
        requester = f"({REVIEW_REQUESTER})" if ledger.has_event_context(self.db) else "NULL"
        # Filter within each task's history so SQLite can use the task index
        # instead of scanning all events with a given action for every request.
        rows = self.db.execute(
            f"""
            WITH current AS ({ledger.current_tasks_sql(self.db)} {project_filter}),
            requests AS (
                SELECT 'peer' AS kind, request.id, created.at, tasks.id AS source_id,
                       {TASK_REFERENCE} AS source_key, tasks.title AS source_title,
                       ask.requester, ask.recipient, tasks.dri AS source_owner, request.state, request.display_state,
                       request.context, request.current_action,
                       (SELECT COUNT(*) FILTER (WHERE action = 'start') > 0
                        FROM events WHERE task_id = request.id) AS has_started,
                       response.id AS result_event_id, response.at AS response_at,
                       request.event_id AS resolution_id, request.at AS resolution_at,
                       request.project_prefix || '-' || COALESCE(request.project_number, request.id) AS request_key,
                       request.title AS request_title, created.note AS request_note, created.links AS request_links,
                       json_extract(created.context, '$.manual_review.pull') AS manual_pull,
                       (ask.review_run_id IS NOT NULL OR json_extract(created.context, '$.manual_review') IS NOT NULL OR (
                           SELECT COUNT(*) FILTER (
                               WHERE action = 'log' AND json_extract(context, '$.stamp_review') IS NOT NULL
                           ) > 0 FROM events WHERE task_id = request.id)) AS is_review
                FROM peer_asks AS ask
                JOIN current AS request ON request.id = ask.task_id
                JOIN tasks ON tasks.id = ask.source_task_id AND tasks.project_prefix = request.project_prefix
                JOIN events AS created ON created.id = (
                    SELECT MIN(id) FROM events WHERE task_id = request.id AND action = 'add')
                LEFT JOIN events AS response ON response.id = ask.result_event_id AND response.task_id = request.id
                UNION ALL
                SELECT 'wait', tasks.event_id, tasks.at, tasks.id, {TASK_REFERENCE}, tasks.title,
                       event.actor, peer.ledger_key, tasks.dri, 'waiting', NULL, NULL, NULL, FALSE,
                       NULL, NULL, NULL, NULL, NULL, NULL, event.note, event.links, NULL, FALSE
                FROM current AS tasks
                JOIN events AS event ON event.id = tasks.event_id
                {self._PEER_WAIT_JOIN}
                WHERE {self._PEER_WAIT_CONDITIONS} AND json_extract(tasks.context, '$.peer_ask_ticket') IS NULL
                UNION ALL
                SELECT 'review', worker_runs.id, worker_runs.assigned_at, tasks.id, {TASK_REFERENCE}, tasks.title,
                       {requester}, worker_runs.worker, tasks.dri,
                       CASE WHEN worker_runs.status = 'planned' AND tasks.display_state <> 'closed' THEN 'asked'
                            WHEN tasks.display_state = 'closed' AND worker_runs.status IN ('planned', 'running')
                                THEN 'closed'
                            WHEN worker_runs.status = 'running' AND ({RECORDED_START}) THEN 'in_progress'
                            WHEN worker_runs.status = 'running' THEN 'start_unconfirmed'
                            WHEN worker_runs.status = 'reported' THEN 'acknowledgment'
                            WHEN worker_runs.status = 'collected' THEN 'done'
                            WHEN worker_runs.status = 'stopped' THEN 'canceled' END,
                       NULL, NULL, NULL, FALSE, response.id, response.at, resolved.id, resolved.at,
                       NULL, NULL, worker_runs.brief, NULL, NULL, TRUE
                FROM worker_runs JOIN current AS tasks ON tasks.id = worker_runs.task_id
                LEFT JOIN events AS response ON response.id = (
                    SELECT MAX(id) FROM events WHERE task_id = tasks.id AND action = 'log'
                    AND json_extract(context, '$.worker_report_id') = worker_runs.id)
                LEFT JOIN events AS resolved ON resolved.id = (
                    SELECT MAX(id) FROM events WHERE task_id = tasks.id AND action = 'log'
                    AND json_extract(context, '$.worker_disposition_id') = worker_runs.id)
                WHERE worker_runs.role = 'review'
                  AND NOT EXISTS (SELECT 1 FROM peer_asks WHERE review_run_id = worker_runs.id)
            )
            SELECT * FROM requests {where}
            ORDER BY at DESC, kind DESC, id DESC LIMIT ?
            """, (*args, limit + 1),
        ).fetchall()
        visible = rows[:limit]
        keys = {row[person] for row in visible for person in ("requester", "recipient", "source_owner") if row[person]}
        people = profiles.visible_by_keys(self.db, keys, self.base)
        return {
            "items": [request_log_item(row, people, self.db, recover_title=recover_title) for row in visible],
            "next_cursor": json.dumps([visible[-1][key] for key in ("at", "kind", "id")])
            if len(rows) > limit else None,
        }

    def recent_report_outcomes(self, owner, project, *, exclude_requests=""):
        # Read each action in event ID order before combining the small results.
        recent_ids = f"""
            SELECT events.id FROM events JOIN tasks ON tasks.id = events.task_id
            WHERE events.action = ? AND tasks.dri = ? AND tasks.project_prefix = ?
                AND {ledger.work_records_sql(self.db)} {exclude_requests}
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

    def reports(self, *, view, before, limit, project=projects.DEFAULT, hide_requests=False, personal_owner=None):
        if project is None:
            return self.all_project_reports(view, before, limit, hide_requests, personal_owner)
        rows, order, render = self.project_reports(view, before, limit, project, hide_requests, personal_owner)
        return page(rows, limit, order, render)

    def report_projects(self):
        return [row["prefix"] for row in self.db.execute("SELECT prefix FROM projects ORDER BY prefix")]

    def all_project_reports(self, view, before, limit, hide_requests, personal_owner):
        scopes = {}
        owners = {}
        scoped_view = "recent" if view == "recent" else "all"
        for project in self.report_projects():
            rows, _, render = self.project_reports(scoped_view, None, None, project, hide_requests, personal_owner)
            scopes[project] = render
            for row in rows:
                owners.setdefault(row["dri"], {})[project] = row

        def cursor(owner, rows):
            if personal_owner == owner:
                return "0:" + owner
            if view == "all" or personal_owner:
                rank = int(not any(row["is_active"] for row in rows.values())) + int(bool(personal_owner))
                return f"{rank}:{owner}"
            return owner

        ordered = sorted(
            (cursor(owner, rows), owner) for owner, rows in owners.items()
            if self.report_owner_visible(owner, rows, view, personal_owner)
        )
        remaining = [(key, owner) for key, owner in ordered if before is None or key > before]
        items = []
        for _, owner in remaining[:limit]:
            project_rows = owners[owner]
            entries = [scopes[project]([row], combining=True)[0] for project, row in project_rows.items()]
            items.append(self.combine_project_reports(owner, project_rows, entries, hide_requests))
        return {
            "items": items,
            "next_cursor": remaining[limit - 1][0] if len(remaining) > limit else None,
        }

    def combine_project_reports(self, owner, project_rows, entries, hide_requests):
        work = sorted((row for entry in entries for row in entry["ranked_work"]), key=lambda row: row[:2])
        outcomes = sorted((event for entry in entries for event in entry["recent_outcomes"]),
                          key=lambda event: event["id"], reverse=True)
        waits = sorted((wait for entry in entries for wait in entry["legacy_peer_waits"]),
                       key=lambda wait: wait["id"], reverse=True)
        activity = [entry["last_activity"] for entry in entries if entry["last_activity"]]
        as_of = [entry["as_of"] for entry in entries if entry["as_of"]]
        report_count = sum(entry["report_count"] for entry in entries)
        pending = self.pending_reports(
            owner, project_prefixes=project_rows, unlinked=True, hide_requests=hide_requests,
        ) if report_count else {"items": [], "next_cursor": None}
        return {
            "owner": owner,
            "projects": list(project_rows),
            "profile": next((entry["profile"] for entry in entries if entry["profile"]), None),
            "goals": [goal for entry in entries for goal in entry["goals"]],
            "as_of": max(as_of, default=None),
            "last_activity": max(activity, key=lambda event: event["id"], default=None),
            "tasks": [task for _, _, task in work[:10]],
            "legacy_peer_waits": waits[:10],
            "task_count": sum(entry["task_count"] for entry in entries),
            "task_counts": {state: sum(entry["task_counts"][state] for entry in entries) for state in WORK_STATES},
            "recent_outcomes": outcomes[:3],
            "worker_counts": {key: sum(entry["worker_counts"][key] for entry in entries)
                              for key in entries[0]["worker_counts"]},
            "pending_reports": pending["items"],
            "pending_reports_before": pending["next_cursor"],
            "report_count": report_count,
        }

    def project_reports(self, view, before, limit, project, hide_requests, personal_owner, *, owner=None):
        current = ledger.current_tasks_sql(self.db) + " WHERE tasks.project_prefix = ?"
        peer_work = watches.has_table(self.db, "peer_asks")
        exclude_requests = ""
        if hide_requests and peer_work:
            exclude_requests = "AND NOT EXISTS (SELECT 1 FROM peer_asks WHERE peer_asks.task_id = tasks.id)"
            current += " " + exclude_requests
        active_state = f"state IN ({ACTIVE_STATES})"
        unlinked_reports = ""
        if peer_work:
            unlinked_reports = "AND NOT EXISTS (SELECT 1 FROM peer_asks WHERE review_run_id = worker_runs.id)"
            if not hide_requests:
                active_state += " OR (state = 'ready' AND id IN (SELECT task_id FROM peer_asks))"
                active_state += """ OR (state = 'parked' AND EXISTS (
                    SELECT 1 FROM peer_asks WHERE task_id = current.id
                      AND recipient = current.dri AND result_event_id IS NULL))"""
        work_columns = ", ".join(f"COALESCE(work.{state}, 0) AS {state}" for state in WORK_STATES)
        roster = project == projects.DEFAULT and self.project_roster is not None
        names = projects.roster_owners(self.db, self.project_roster) if roster else {}
        local_dris = watches.has_table(self.db, "local_dris")
        team_only = "AND ledger_key NOT IN (SELECT actor FROM local_dris)" if local_dris else ""
        owner_source = f"""SELECT dri FROM project_tasks WHERE dri <> ''
                           UNION SELECT ledger_key FROM board_principals WHERE kind = 'agent' AND ? = 'T' {team_only}
                           UNION {projects.PROJECT_OWNERS}
                           UNION SELECT recipient FROM legacy_waits"""
        owner_args = (project, project, project)
        local_owners = ()
        if local_dris:
            owner_source += " UNION SELECT actor FROM local_dris WHERE project = ?"
            owner_args += (project,)
            if roster:
                local_owners = tuple(row["actor"] for row in self.db.execute(
                    "SELECT actor FROM local_dris WHERE project = ?", (project,),
                ))
        if roster:
            owner_args = (*names, *local_owners)
            if personal_owner and personal_owner not in names:
                owner_args += (personal_owner,)
            if not owner_args and view != "recent":
                return [], "dri", lambda visible: []
            owner_source = ("VALUES " + ", ".join("(?)" for _ in owner_args)
                            if owner_args else "SELECT CAST(NULL AS TEXT) WHERE FALSE")
        elif personal_owner:
            owner_source += " UNION SELECT ?"
            owner_args += (personal_owner,)
        order = "cursor" if view == "all" or personal_owner else "dri"
        filter_args = () if view == "all" else (view == "active",)
        view_filter = "" if view == "all" else " AND is_active = ?"
        rank = "CASE WHEN is_active THEN '0:' ELSE '1:' END"
        rank_args = ()
        if personal_owner:
            rank = "CASE WHEN dri = ? THEN '0:' WHEN is_active THEN '1:' ELSE '2:' END"
            rank_args = (personal_owner,)
            if view not in ("all", "recent"):
                view_filter = " AND (is_active = ? OR dri = ?)"
                filter_args += (personal_owner,)
        recent_cte, recent_args = "", ()
        recent_cutoff = None
        summary_owners = "owners"
        if view == "recent":
            recent_cutoff = (datetime.fromisoformat(ledger.timestamp()) - timedelta(hours=1)).isoformat(
                timespec="seconds",
            ).replace("+00:00", "Z")
            linked_reviews = ""
            if peer_work and not hide_requests:
                linked_reviews = """
                    UNION SELECT current.dri
                    FROM current JOIN peer_asks ON peer_asks.task_id = current.id
                    JOIN tasks AS source ON source.id = peer_asks.source_task_id
                      AND source.project_prefix = current.project_prefix
                    WHERE current.state IN ('ready', 'waiting', 'in_progress', 'parked')
                      AND (peer_asks.review_run_id IS NOT NULL OR (
                          SELECT COUNT(*) FILTER (
                              WHERE (action = 'add' AND json_extract(context, '$.manual_review') IS NOT NULL)
                                 OR (action = 'log' AND json_extract(context, '$.stamp_review') IS NOT NULL)
                          ) > 0 FROM events WHERE task_id = current.id))
                """
            recent_cte = f"""recent_signals AS (
                SELECT current.dri
                FROM current JOIN board_principals AS person ON person.ledger_key = current.dri
                JOIN events AS progress ON progress.id = ({self.owner_progress_event('current')})
                WHERE current.state IN ('waiting', 'in_progress') AND progress.at >= ?
                  AND (person.kind = 'human' OR (person.kind = 'agent' AND current.dri LIKE 'local-dri:%'))
                UNION SELECT dri FROM current WHERE state = 'human_review'
                UNION SELECT current.dri FROM current JOIN worker_runs ON worker_runs.task_id = current.id
                WHERE current.display_state <> 'closed' AND worker_runs.role = 'review'
                  AND worker_runs.status IN ('planned', 'running', 'reported') {unlinked_reports}
                {linked_reviews}
            ),"""
            recent_args = (recent_cutoff,)
            eligible = [
                "dri IN (SELECT dri FROM recent_signals)",
                """dri IN (SELECT ledger_key FROM board_principals
                           WHERE kind = 'agent' AND ledger_key NOT LIKE 'local-dri:%')""",
            ]
            if roster:
                summary_owners = "(SELECT dri FROM owners UNION SELECT dri FROM recent_signals) AS owners"
                eligible.append("""dri IN (
                    SELECT dri FROM owners WHERE dri NOT LIKE 'local-dri:%'
                      AND dri NOT IN (SELECT ledger_key FROM board_principals WHERE kind = 'human')
                )""")
            view_filter = " AND (" + " OR ".join(eligible) + ")"
            filter_args = ()
        owner_filter = "" if owner is None else " AND dri = ?"
        owner_filter_args = () if owner is None else (owner,)
        paging = "" if limit is None else "LIMIT ?"
        paging_args = () if limit is None else (limit + 1,)
        rows = self.db.execute(
            f"""
            WITH current AS ({current}),
                 project_tasks AS (
                     SELECT id, dri FROM tasks WHERE project_prefix = ? AND {ledger.work_records_sql(self.db)}
                 ),
                 legacy_waits AS (
                     SELECT peer.ledger_key AS recipient, MAX(tasks.at) AS updated
                     FROM current AS tasks {self._PEER_WAIT_JOIN}
                     WHERE {self._PEER_WAIT_CONDITIONS}
                       AND json_extract(tasks.context, '$.peer_ask_ticket') IS NULL
                     GROUP BY peer.ledger_key
                 ),
                 work AS (
                     SELECT dri, {WORK_COUNTS},
                            COUNT(CASE WHEN {active_state} THEN 1 END) AS task_count,
                            MAX(CASE WHEN {active_state} THEN at END) AS updated
                     FROM current GROUP BY dri
                 ),
                 reports AS (
                     SELECT current.dri, COUNT(*) AS report_count, MAX(worker_runs.reported_at) AS updated
                     FROM worker_runs JOIN current ON current.id = worker_runs.task_id
                     WHERE worker_runs.status = 'reported' AND current.display_state <> 'closed'
                       {unlinked_reports}
                     GROUP BY current.dri
                 ),
                 runs AS (
                     SELECT current.dri, {WORKER_COUNTS}
                     FROM worker_runs JOIN current ON current.id = worker_runs.task_id
                     GROUP BY current.dri
                 ),
                 owners(dri) AS ({owner_source}),
                 {recent_cte}
                 summary AS (
                     SELECT owners.dri, {work_columns}, COALESCE(work.task_count, 0) AS task_count,
                            COALESCE(reports.report_count, 0) AS report_count,
                            COALESCE(runs.recorded_runs, 0) AS recorded_runs,
                            COALESCE(runs.running_builds, 0) AS running_builds,
                            COALESCE(runs.launched_builds, 0) AS launched_builds,
                            COALESCE(runs.running_reviews, 0) AS running_reviews,
                            COALESCE(runs.launched_reviews, 0) AS launched_reviews,
                            legacy_waits.recipient IS NOT NULL AS has_legacy_waits,
                            NULLIF({tasks_storage.greatest(self.db)}(
                                COALESCE(work.updated, ''), COALESCE(reports.updated, ''), COALESCE(legacy_waits.updated, '')
                            ), '') AS as_of,
                            (COALESCE(work.task_count, 0) > 0 OR reports.dri IS NOT NULL
                                OR legacy_waits.recipient IS NOT NULL
                                OR COALESCE(runs.running_builds, 0) > 0
                                OR COALESCE(runs.running_reviews, 0) > 0) AS is_active
                     FROM {summary_owners}
                     LEFT JOIN work ON work.dri = owners.dri
                     LEFT JOIN reports ON reports.dri = owners.dri
                     LEFT JOIN runs ON runs.dri = owners.dri
                     LEFT JOIN legacy_waits ON legacy_waits.recipient = owners.dri
                 ),
                 ordered AS (
                     SELECT summary.*, ({rank}) || dri AS cursor
                     FROM summary
                 )
            SELECT * FROM ordered WHERE {order} > ?{view_filter}{owner_filter}
            ORDER BY {order} {paging}
            """, (project, project, *owner_args, *recent_args, *rank_args, before or "", *filter_args,
                  *owner_filter_args, *paging_args),
        ).fetchall()

        def render(visible, *, combining=False):
            people = [row["dri"] for row in visible]
            directory = profiles.visible_by_keys(self.db, people, self.base)
            goal_summaries = goals.report_summaries(self.db, project, people)
            results = []
            for row in visible:
                person = row["dri"]
                profile = directory.get(person)
                active = []
                if row["task_count"]:
                    work_order = "CASE state WHEN 'human_review' THEN 0 WHEN 'waiting' THEN 1 WHEN 'parked' THEN 1 ELSE 2 END"
                    work_args = ()
                    if recent_cutoff and profile and (profile["kind"] == "human" or profile.get("dri_kind") == "local"):
                        work_order = f"""CASE WHEN state = 'human_review' THEN 0
                            WHEN state IN ('waiting', 'in_progress') AND (
                                SELECT at FROM events AS progress
                                WHERE progress.id = ({self.owner_progress_event('current')})
                            ) >= ? THEN 1
                            WHEN state IN ('waiting', 'parked') THEN 2 ELSE 3 END"""
                        work_args = (recent_cutoff,)
                    active = self.db.execute(
                        f"""WITH current AS ({current})
                            SELECT current.*, {work_order} AS report_rank
                            FROM current WHERE dri = ? AND ({active_state})
                            ORDER BY report_rank, event_id DESC LIMIT 10""",
                        (project, *work_args, person),
                    ).fetchall()
                pending = self.pending_reports(person, project=project, unlinked=True) if (
                    not combining and row["report_count"]
                ) else {"items": [], "next_cursor": None}
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
                    "legacy_peer_waits": self.queued_peer_waits(
                        before=None, limit=10, project=project, legacy_recipient=person,
                    )["items"] if row["has_legacy_waits"] else [],
                    "task_count": row["task_count"],
                    "task_counts": {state: row[state] for state in WORK_STATES},
                    "recent_outcomes": self.recent_report_outcomes(person, project, exclude_requests=exclude_requests),
                    "worker_counts": worker_counts(row),
                    "pending_reports": pending["items"],
                    "pending_reports_before": pending["next_cursor"],
                    "report_count": row["report_count"],
                    **({"ranked_work": [
                        (source["report_rank"], -source["event_id"], task)
                        for source, task in zip(active, owned, strict=True)
                    ]} if combining else {}),
                })
            return results

        return rows, order, render

    def report_project_prefixes(self, owner, view, hide_requests, personal_owner):
        scoped_view = "recent" if view == "recent" else "all"
        scopes = {}
        for project in self.report_projects():
            rows, _, _ = self.project_reports(scoped_view, None, 1, project, hide_requests, personal_owner, owner=owner)
            if rows:
                scopes[project] = rows[0]
        return list(scopes) if self.report_owner_visible(owner, scopes, view, personal_owner) else []

    def report_owner_visible(self, owner, rows, view, personal_owner):
        return (view in ("all", "recent") or owner == personal_owner
                or any(row["is_active"] for row in rows.values()) == (view == "active"))

    def goal_activity(self, project):
        """Summarize observed goal starts, outcomes and idle offers in one project."""
        roster = None
        if project == projects.DEFAULT and self.project_roster is not None:
            roster = projects.roster_owners(self.db, self.project_roster)
            if not roster:
                return {"items": []}
        roster_filter = ""
        if roster:
            marks = ", ".join("?" for _ in roster)
            roster_filter = f"AND tasks.dri IN ({marks})"
        context = "events.context" if ledger.has_event_context(self.db) else "'{}'"
        rows = self.db.execute(
            f"""
            SELECT tasks.id, {TASK_REFERENCE} AS key, tasks.dri AS owner, tasks.title AS summary,
                   goal.kind, goal.deadline, events.id AS event_id, events.action, events.state,
                   events.at, events.note, events.links, {context} AS context
            FROM task_goals AS goal
            JOIN tasks ON tasks.id = goal.task_id
            LEFT JOIN events ON events.task_id = tasks.id AND events.action NOT IN ('source-event', 'log')
            WHERE tasks.project_prefix = ? AND goal.retired = 0 {roster_filter}
            ORDER BY tasks.dri, CASE goal.kind WHEN 'standing' THEN 0 ELSE 1 END,
                     goal.deadline, tasks.title, tasks.id, events.id
            """, (project, *(roster or ())),
        )
        goals_by_id = {}
        for event, review in goals.review_events(rows):
            number = event["id"]
            if number not in goals_by_id:
                goals_by_id[number] = {
                    **{key: event[key] for key in ("id", "key", "owner", "summary", "kind", "deadline")},
                    "project": project, "starts": 0, "completed_checks": 0,
                    "last_completed_at": None, "outcomes": deque(maxlen=GOAL_OUTCOME_LIMIT),
                }
            row = goals_by_id[number]
            if review == "start":
                row["starts"] += 1
            elif review == "completed":
                row["completed_checks"] += 1
                row["last_completed_at"] = event["at"]
                row["outcomes"].append({
                    "id": event["event_id"], "at": event["at"], "kind": event["action"], "note": event["note"],
                    "links": self.links(json.loads(event["links"]), event_id=event["event_id"]),
                })
        people = profiles.visible_by_keys(self.db, [row["owner"] for row in goals_by_id.values()], self.base)
        offers = goal_offers.idle_offer_counts(self.db, list(goals_by_id))
        for number, row in goals_by_id.items():
            owner = row["owner"]
            profile = people.get(owner)
            if profile is None and roster and owner in roster:
                profile = projects.roster_profile(owner, roster[owner])
            row.update(profile=profile, idle_offers=offers.get(number), outcomes=list(reversed(row["outcomes"])))
        return {"items": list(goals_by_id.values())}

    def assignee(self, owner, *, project=projects.DEFAULT):
        profile = profiles.visible_by_keys(self.db, [owner], self.base).get(owner)
        if profile is None and project == projects.DEFAULT and self.project_roster:
            name = projects.roster_owners(self.db, self.project_roster).get(owner)
            if name:
                profile = projects.roster_profile(owner, name)
        if profile is None and not self.db.execute(
            f"SELECT 1 FROM tasks WHERE dri = ? AND {ledger.work_records_sql(self.db)} LIMIT 1", (owner,),
        ).fetchone():
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
            f"WHERE tasks.dri = ? AND tasks.project_prefix = ? AND {OUTCOME_ACTIONS}"
            f" AND {ledger.work_records_sql(self.db)} LIMIT 1", (owner, project),
        ).fetchone() is not None
        return {
            "profile": profile, "report_count": count,
            "goal": goals.get(self.db, project, owner),
            "milestones": goals.milestones(self.db, project, owner, include_ticket=True),
            "standing_goal": next(iter(goals.standing(self.db, owner, project, include_ticket=True)), None),
            "pending_reports": reports["items"], "pending_reports_before": reports["next_cursor"],
            "in_progress": task_counts["in_progress"] > 0,
            "task_counts": task_counts,
            "has_outcomes": has_outcomes,
            "worker_counts": worker_counts(counts),
        }

    def pending_reports(self, person, *, before=None, limit=10, project=None, project_prefixes=None, unlinked=False,
                        hide_requests=False):
        if project_prefixes is not None and not project_prefixes:
            return {"items": [], "next_cursor": None}
        scope = "(? IS NULL OR tasks.project_prefix = ?)"
        scope_args = (project, project)
        if project_prefixes is not None:
            marks = ", ".join("?" for _ in project_prefixes)
            scope = f"tasks.project_prefix IN ({marks})"
            scope_args = tuple(project_prefixes)
        unlinked_filter = ""
        if (unlinked or hide_requests) and watches.has_table(self.db, "peer_asks"):
            if unlinked:
                unlinked_filter = "AND NOT EXISTS (SELECT 1 FROM peer_asks WHERE review_run_id = worker_runs.id)"
            if hide_requests:
                unlinked_filter += " AND NOT EXISTS (SELECT 1 FROM peer_asks WHERE peer_asks.task_id = tasks.id)"
        runs = self.db.execute(
            f"""
            WITH current AS ({ledger.current_tasks_sql(self.db)})
            SELECT worker_runs.*, tasks.title AS task_title, {TASK_REFERENCE} AS task_key
            FROM worker_runs JOIN tasks ON tasks.id = worker_runs.task_id
            JOIN current ON current.id = tasks.id
            WHERE tasks.dri = ? AND worker_runs.status = 'reported'
                AND current.display_state <> 'closed'
                AND worker_runs.id < ? AND {scope}
            {unlinked_filter}
            ORDER BY worker_runs.id DESC LIMIT ?
            """, (person, before or 2**63 - 1, *scope_args, limit + 1),
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
        args = [project, project, recipient, project, project, creations, recipient, recipient, recipient,
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
                SELECT events.at, 'event' AS source, events.id, 'creation' AS kind
                FROM events JOIN visible_tasks ON visible_tasks.id = events.task_id
                JOIN current_tasks ON current_tasks.id = events.task_id
                WHERE action = 'add' AND ?
                UNION ALL
                SELECT events.at, 'event', events.id, 'watched'
                FROM task_watches JOIN events ON events.task_id = task_watches.task_id
                JOIN visible_tasks ON visible_tasks.id = events.task_id
                WHERE task_watches.watcher = ? AND events.id > task_watches.after_event
                  AND events.action NOT IN ('add', 'assign-human', 'redirect-decision', 'withdraw-decision', 'decide')
                UNION ALL
                SELECT events.at, 'event', events.id, 'watched'
                FROM extension_notifications JOIN events ON events.id = extension_notifications.event_id
                JOIN visible_tasks ON visible_tasks.id = events.task_id
                WHERE extension_notifications.recipient = ? AND NOT EXISTS (
                    SELECT 1 FROM task_watches WHERE task_watches.task_id = events.task_id
                    AND task_watches.watcher = ? AND events.id > task_watches.after_event
                )
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

    def actionable_work(self, owner, *, today, project, ordinary_only=False):
        """Yield the first actionable item in each queue in pickup order."""
        scope = {"project": project, "limit": 1}
        queues = (
            ("ready", self.tasks, {"dri": owner, "state": "ready", "priority_order": True}),
            ("review", self.actionable_reviews, {"person": owner, "before": None, "unlinked": True}),
            ("report", self.pending_reports, {"person": owner, "unlinked": True}),
            ("readiness", self.readiness, {"owner": owner, "before": None}),
            ("request", self.attention, {"recipient": owner, "state": "open", "today": today, "before": None}),
            ("decision", self.tasks, {"waiting_on": owner, "state": "human_review"}),
            ("reminder", self.tasks, {"dri": owner, "state": "parked", "review_due": today}),
            ("in_progress", self.tasks, {"dri": owner, "state": "in_progress"}),
            ("shared_ready", self.tasks, {"dri": "", "state": "ready", "priority_order": True, "managed_by": owner}),
        )
        for kind, query, filters in queues:
            if ordinary_only and kind in ("reminder", "in_progress"):
                filters["exclude_goals"] = True
            for item in query(**scope, **filters)["items"]:
                yield {
                    "kind": kind,
                    "id": item.get("task_id", item.get("id")),
                    "key": item.get("task_key") or item["key"],
                    "title": item.get("task_title") or item["title"],
                }

    def needs_you(self, actor, *, today, project, before, limit):
        owner = actor["ledger_key"]
        decision_before, request_before, review_before = before or (None, None, None)
        decisions = requests = reviews = {"items": [], "next_cursor": None}
        if actor["kind"] == "human" and (before is None or decision_before is not None):
            decisions = self.tasks(state="human_review", waiting_on=owner, project=project,
                                   before=decision_before, limit=limit)
        if actor["kind"] == "human" and (before is None or request_before is not None):
            requests = self.attention(owner, state="open", today=today, before=request_before,
                                      limit=limit, project=project)
        if before is None or review_before is not None:
            reviews = self.request_log(project=project, before=review_before, limit=limit,
                                       actionable_reviewer=owner, recover_title=True)

        decision_rows = decisions["items"]
        actors = {}
        if decision_rows:
            ids = [item["event_id"] for item in decision_rows]
            placeholders = ", ".join("?" for _ in ids)
            actors = dict(self.db.execute(f"SELECT id, actor FROM events WHERE id IN ({placeholders})", ids))
        people = profiles.visible_by_keys(self.db, set(actors.values()), self.base)
        items = [
            {
                "id": f"decision:{item['event_id']}", "kind": "decision",
                "ticket": item["key"], "title": item["title"],
                "requester": actors.get(item["event_id"]),
                "requester_profile": people.get(actors.get(item["event_id"])),
            }
            for item in decision_rows
        ]
        items.extend(
            {
                "id": f"request:{item['id']}", "kind": "request",
                "ticket": item["task_key"], "title": item["task_title"],
                "requester": item["sender"], "requester_profile": item["sender_profile"],
            }
            for item in requests["items"]
        )
        items.extend(
            {
                "id": f"review:{item['id']}", "kind": "review",
                "ticket": item["request_key"] or item["source_key"],
                "title": item["request_title"] or item["source_title"],
                "requester": item["requester"], "requester_profile": item["requester_profile"],
            }
            for item in reviews["items"]
        )
        cursors = [result["next_cursor"] for result in (decisions, requests, reviews)]
        return {"items": items, "next_cursor": json.dumps(cursors) if any(cursors) else None}

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
