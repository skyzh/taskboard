"""A project assignee's dated and standing goals."""

import json
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import tasks_core as ledger
import tasks_projects as projects
import tasks_ring1 as ring1
import tasks_storage
from taskboard_client.work import task_title


SCHEMA = """
    CREATE TABLE IF NOT EXISTS board_project_goals (
        project TEXT NOT NULL REFERENCES projects (prefix),
        owner TEXT NOT NULL REFERENCES board_principals (ledger_key),
        summary TEXT NOT NULL,
        deadline TEXT NOT NULL,
        ticket_ids TEXT NOT NULL,
        PRIMARY KEY (project, owner)
    )
"""

STANDING_SCHEMA = """
    CREATE TABLE IF NOT EXISTS board_standing_goals (
        project TEXT NOT NULL REFERENCES projects (prefix),
        owner TEXT NOT NULL REFERENCES board_principals (ledger_key),
        summary TEXT NOT NULL,
        PRIMARY KEY (project, owner)
    )
"""
STANDING_LIMIT = 500
CRITERION_LIMIT = 500
EXPLORATION_SETTING = "standing_exploration:"
PACIFIC = ZoneInfo("America/Los_Angeles")

MILESTONE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS board_goal_milestones (
        project TEXT NOT NULL REFERENCES projects (prefix),
        owner TEXT NOT NULL REFERENCES board_principals (ledger_key),
        summary TEXT NOT NULL,
        deadline TEXT NOT NULL,
        ticket_ids TEXT NOT NULL,
        PRIMARY KEY (project, owner, summary)
    )
"""

GOAL_TICKET_SCHEMA = """
    CREATE TABLE IF NOT EXISTS task_goals (
        task_id INTEGER PRIMARY KEY REFERENCES tasks (id),
        kind TEXT NOT NULL CHECK (kind IN ('standing', 'original', 'milestone')),
        deadline TEXT,
        ticket_ids TEXT NOT NULL,
        retired INTEGER NOT NULL DEFAULT 0
    )
"""
GOAL_MIGRATION = "goal_tickets_migrated"
LEGACY_GOAL_STEP = "Start or link a concrete investigation when there is work to do on this goal."
DATED_GOAL_STEP = "Review progress toward this milestone and record the result by its deadline."
# Existing goal creation events may carry the previous generated instructions.
PREVIOUS_STANDING_EXPLORATION = (
    "Start this goal ticket if this turn still allows exploration, and note what you plan to check. "
    "Explore the product, recent changes, or workflow evidence. "
    "Open or link a focused ticket if you find a bug or improvement. Otherwise, record what you checked. "
    "If you open a ticket from this standing goal, add the spontaneous tag."
)
STANDING_EXPLORATION_LABEL = "Explore this goal"
STANDING_EXPLORATION_START = (
    "Finish actionable work and reviews first. If your Inbox is clear and you have not explored this turn, "
    "start this goal ticket for one focused pass and say what you will check."
)
STANDING_EXPLORATION = (
    f"{STANDING_EXPLORATION_START} Explore the product, recent changes, or workflow evidence. "
    "If you find a bug or improvement, search for an existing ticket before opening a focused one. "
    "If you open a ticket from this standing goal, add the spontaneous tag. "
    "Record the result and park this goal ticket. The completed pass counts as today's Pacific daily review. "
    "Recheck Inbox for actionable work and end exploration for the turn."
)
KEEP_EXPLORING = (
    "Check actionable Inbox work and assigned reviews before each pass. "
    "Choose a distinct check grounded in recent feedback, a finished ticket, or current product evidence. "
    "Verify a concrete finding and reuse an existing ticket before opening a new one. "
    "Make focused fixes within your ordinary permissions. Record a no-finding result only after an actual check. "
    "Record each result on this goal and update the handoff, then recheck Inbox and reviews. "
    "If the queue stays clear, continue with a distinct check. If actionable work arrives, record your checkpoint "
    "and switch to it. Use `update --standing-pass` to record a completed check. "
    "If the written criterion is met, record the evidence with `park --criterion-met` until the next daily review."
)


def ensure_extended_schema(db):
    db.execute(STANDING_SCHEMA)
    db.execute(MILESTONE_SCHEMA)


def migrate_goal_tickets(db):
    """Carry legacy project goals into ordinary ticket records in the caller's transaction."""
    db.execute(GOAL_TICKET_SCHEMA)
    if db.execute("SELECT 1 FROM board_settings WHERE key = ?", (GOAL_MIGRATION,)).fetchone():
        return
    rows = db.execute("""
        SELECT project, owner, summary, 'standing' AS kind, NULL AS deadline, '[]' AS ticket_ids
        FROM board_standing_goals
        UNION ALL
        SELECT project, owner, summary, 'original', deadline, ticket_ids FROM board_project_goals
        UNION ALL
        SELECT project, owner, summary, 'milestone', deadline, ticket_ids FROM board_goal_milestones
        ORDER BY project, owner, kind, summary
    """).fetchall()
    for row in rows:
        create_goal_ticket(db, row["project"], row["owner"], row["kind"], row["summary"],
                           row["deadline"], row["ticket_ids"])
    if rows:
        db.execute("INSERT INTO board_settings (key, value) VALUES (?, '1')", (GOAL_MIGRATION,))
        ledger.bump_revision(db)


def restore_legacy_goals(db):
    """Project current goals for an older server before reverting the application."""
    with tasks_storage.write(db):
        if not tasks_storage.table_columns(db, "task_goals"):
            return 0
        owners = [row[0] for row in db.execute(
            "SELECT DISTINCT tasks.dri FROM tasks JOIN task_goals ON tasks.id = task_goals.task_id"
        )]
        rows = goal_rows(db, owners)
        migrated = db.execute("SELECT 1 FROM board_settings WHERE key = ?", (GOAL_MIGRATION,)).fetchone()
        if not rows and not migrated:
            return 0
        for table in ("board_standing_goals", "board_project_goals", "board_goal_milestones"):
            db.execute(f"DELETE FROM {table}")
        for row in rows:
            common = (row["project_prefix"], row["owner"], row["summary"])
            if row["kind"] == "standing":
                db.execute("INSERT INTO board_standing_goals VALUES (?, ?, ?)", common)
            else:
                table = "board_project_goals" if row["kind"] == "original" else "board_goal_milestones"
                db.execute(f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?)",
                           (*common, row["deadline"], row["ticket_ids"]))
            if row["state"] == "parked":
                args = SimpleNamespace(id=row["id"], by=row["owner"], note="Restored as a legacy goal.", related=None)
                ledger.close_task(db, args, [])
            db.execute("UPDATE task_goals SET retired=1 WHERE task_id=?", (row["id"],))
        db.execute("DELETE FROM board_settings WHERE key = ?", (GOAL_MIGRATION,))
        ledger.bump_revision(db)
        return len(rows)


def create_goal_ticket(db, project, owner, kind, summary, deadline, encoded_ids):
    args = SimpleNamespace(
        project=project, dri=owner, by=owner, title=summary, brief="",
        step=STANDING_EXPLORATION if kind == "standing" else DATED_GOAL_STEP,
    )
    task_id = ledger.create_task_record(db, args, [], state="parked")
    db.execute(
        "INSERT INTO task_goals (task_id, kind, deadline, ticket_ids) VALUES (?, ?, ?, ?)",
        (task_id, kind, deadline, encoded_ids),
    )
    return task_id


def parked_guidance(goal, note, standing_exploration):
    """Project the generated note against the current goal, leaving event history intact."""
    if goal["kind"] == "standing":
        return STANDING_EXPLORATION_LABEL, KEEP_EXPLORING if goal["stopping_criterion"] is not None else standing_exploration
    if note not in (LEGACY_GOAL_STEP, DATED_GOAL_STEP):
        return None
    action = ("Review the linked tickets and record progress and blockers"
              if json.loads(goal["ticket_ids"]) else "Check milestone progress and record the outcome")
    deadline = f" by {goal['deadline']}" if goal["deadline"] else ""
    return "Review this milestone", f"{action}{deadline}."


def review_events(events):
    """Classify each event in a goal history as a review start, outcome, or neither."""
    previous, open_checks = {}, set()
    for event in events:
        number = event["id"]
        review = None
        if event["action"] == "start":
            open_checks.add(number)
            review = "start"
        elif (number in open_checks and event["action"] == "update"
              and ledger.event_context(event).get("goal_pass_complete")):
            review = "completed"
        elif number in open_checks and event["state"] in ("done", "parked"):
            open_checks.remove(number)
            # Older reminder edits were stored as Park with the previous note and links.
            copied_reminder = (event["note"], event["links"]) == previous.get(number) and any(
                key in ledger.event_context(event) for key in ("review_after", "defer_until")
            )
            if event["action"] == "done" or (event["action"] == "park" and not copied_reminder):
                review = "completed"
        previous[number] = (event["note"], event["links"])
        yield event, review


def goal_rows(db, owners, project=None, kind=None, *, exclude_watched_by=None):
    if not owners:
        return []
    placeholders = ", ".join("?" for _ in owners)
    filters = [f"tasks.dri IN ({placeholders})", "events.state <> 'done'", "goals.retired = 0"]
    args = list(owners)
    if project is not None:
        filters.append("tasks.project_prefix = ?")
        args.append(project)
    if kind is not None:
        filters.append("goals.kind = ?")
        args.append(kind)
    if exclude_watched_by is not None:
        filters.append("NOT EXISTS (SELECT 1 FROM task_watches WHERE task_id = tasks.id AND watcher = ?)")
        args.append(exclude_watched_by)
    return db.execute(f"""
        SELECT tasks.id, tasks.project_prefix, tasks.project_number, tasks.dri AS owner, tasks.title AS summary,
               goals.kind, goals.deadline, goals.ticket_ids, events.state, events.at, events.context,
               events.id AS event_id,
               exploration.value AS stopping_criterion
        FROM task_goals AS goals JOIN tasks ON tasks.id = goals.task_id
        LEFT JOIN board_settings AS exploration ON exploration.key = '{EXPLORATION_SETTING}' || CAST(tasks.id AS TEXT)
        JOIN events ON events.id = (
            SELECT MAX(id) FROM events WHERE task_id = tasks.id AND action NOT IN ('source-event', 'log')
        )
        WHERE {' AND '.join(filters)}
        ORDER BY tasks.project_prefix,
                 CASE goals.kind WHEN 'standing' THEN 0 WHEN 'original' THEN 1 ELSE 2 END,
                 goals.deadline, tasks.title
    """, args).fetchall()


def standing_summary(row, *, include_ticket=False):
    result = {"project": row["project_prefix"], "summary": row["summary"]}
    if include_ticket:
        result["ticket"] = projects.row_reference(row)
    if row["stopping_criterion"] is not None:
        result.update(keep_exploring=True, stopping_criterion=row["stopping_criterion"])
    return result


def standing(db, owner, project=None, *, include_ticket=False):
    return [standing_summary(row, include_ticket=include_ticket)
            for row in goal_rows(db, [owner], project, "standing")]


def review_slot(day, review_time):
    """Use the first occurrence on a clock repeat and the first valid minute after a gap."""
    local = datetime.combine(day, time.fromisoformat(review_time))
    while True:
        instant = local.replace(tzinfo=PACIFIC).astimezone(timezone.utc)
        if instant.astimezone(PACIFIC).replace(tzinfo=None) == local:
            return instant
        local += timedelta(minutes=1)


def exploration_available(db, row, now):
    context = ledger.event_context(row)
    if (row["state"] != "parked" or not row["stopping_criterion"]
            or not context.get("goal_criterion_met")):
        return True
    schedule = db.execute("SELECT review_time FROM goal_review_times WHERE task_id = ?", (row["id"],)).fetchone()
    if schedule is None:
        return False
    criterion_at = datetime.fromisoformat(context.get("goal_criterion_at", row["at"]))
    next_day = criterion_at.astimezone(PACIFIC).date() + timedelta(days=1)
    return now.astimezone(timezone.utc) >= review_slot(next_day, schedule["review_time"])


def exploration_criterion(db, number):
    saved = db.execute("SELECT value FROM board_settings WHERE key = ?",
                       (EXPLORATION_SETTING + str(number),)).fetchone()
    return saved["value"] if saved is not None else None


def write_goal(db, project, owner, kind, row, goal):
    if goal is None:
        if row is not None:
            args = SimpleNamespace(id=row["id"], by=owner, note="Removed this goal.", related=None)
            ledger.close_task(db, args, [])
        return
    summary, deadline, ids = goal
    if row is None or summary != row["summary"]:
        summary = task_title(summary)
    encoded = json.dumps(ids)
    if row is None:
        create_goal_ticket(db, project, owner, kind, summary, deadline, encoded)
        return
    if (summary, deadline, encoded) == (row["summary"], row["deadline"], row["ticket_ids"]):
        return
    db.execute("UPDATE tasks SET title = ? WHERE id = ?", (summary, row["id"]))
    db.execute("UPDATE task_goals SET deadline = ?, ticket_ids = ? WHERE task_id = ?",
               (deadline, encoded, row["id"]))
    current = ledger.get_task(db, row["id"])
    ledger.record(db, row["id"], "log", owner, current["state"], "Updated this goal.", None, [])


def save_standing(db, project, owner, summary, *, keep_exploring=None, criterion=""):
    row = next(iter(goal_rows(db, [owner], project, "standing")), None)
    write_goal(db, project, owner, "standing", row, (summary, None, []) if summary is not None else None)
    if summary is None:
        if row is not None:
            db.execute("DELETE FROM board_settings WHERE key = ?", (EXPLORATION_SETTING + str(row["id"]),))
        return None
    current = next(iter(goal_rows(db, [owner], project, "standing")))
    if keep_exploring is True:
        db.execute(
            """INSERT INTO board_settings (key, value) VALUES (?, ?)
               ON CONFLICT (key) DO UPDATE SET value = excluded.value WHERE board_settings.value <> excluded.value""",
            (EXPLORATION_SETTING + str(current["id"]), criterion),
        )
    elif keep_exploring is False:
        db.execute("DELETE FROM board_settings WHERE key = ?", (EXPLORATION_SETTING + str(current["id"]),))
    return next(iter(standing(db, owner, project)))


def report_summaries(db, project, owners, *, exclude_watched_by=None):
    summaries = {}
    for row in goal_rows(db, owners, project, exclude_watched_by=exclude_watched_by):
        summaries.setdefault(row["owner"], []).append({
            "kind": row["kind"], "summary": row["summary"], "deadline": row["deadline"],
            "ticket": projects.row_reference(row),
        })
    return summaries


def ticket_details(db, task, *, signed_in=False):
    if not tasks_storage.table_columns(db, "task_goals"):
        return None
    row = db.execute("SELECT kind, deadline, ticket_ids FROM task_goals WHERE task_id=? AND retired=0",
                     (task["id"],)).fetchone()
    if row is None:
        return None
    result = {"kind": row["kind"], "deadline": row["deadline"],
              "tickets": tickets(db, projects.row_project(task), task["dri"], json.loads(row["ticket_ids"]))}
    resume_at_review = False
    if row["kind"] == "standing" and (criterion := exploration_criterion(db, task["id"])) is not None:
        result.update(keep_exploring=True, stopping_criterion=criterion)
        resume_at_review = task["state"] == "parked" and ledger.event_context(task).get("goal_criterion_met")
    if (signed_in and task["state"] != "done") or resume_at_review:
        schedule = db.execute("SELECT review_time FROM goal_review_times WHERE task_id = ?", (task["id"],)).fetchone()
        review_time = schedule["review_time"] if schedule else None
        if signed_in and review_time:
            result["review_time"] = review_time
        if resume_at_review:
            result["resume_review_time"] = review_time
    if (row["kind"] == "standing" and task["state"] == "parked"
            and task["note"] not in (LEGACY_GOAL_STEP, PREVIOUS_STANDING_EXPLORATION, STANDING_EXPLORATION)):
        result["parked_note"] = task["note"]
    return result


def tickets(db, project, owner, ids):
    if not ids:
        return []
    placeholders = ", ".join("?" for _ in ids)
    rows = db.execute(
        f"""
        SELECT id, title, project_prefix, project_number
        FROM tasks
        WHERE project_prefix = ? AND dri = ? AND id IN ({placeholders})
        """,
        (project, owner, *ids),
    )
    by_id = {
        row["id"]: {"id": row["id"], "key": projects.row_reference(row), "title": row["title"]}
        for row in rows
    }
    return [by_id[number] for number in ids if number in by_id]


def get(db, project, owner):
    row = next(iter(goal_rows(db, [owner], project, "original")), None)
    return dated_goal(db, project, owner, row) if row is not None else None


def dated_goal(db, project, owner, row):
    return {
        "summary": row["summary"],
        "deadline": row["deadline"],
        "tickets": tickets(db, project, owner, json.loads(row["ticket_ids"])),
    }


def queue_context(db, owner, project=None, *, principal=None, now=None):
    now = now or datetime.fromisoformat(ledger.timestamp()).astimezone(PACIFIC)
    standing_goals = []
    dated_goals = []
    for row in goal_rows(db, [owner], project):
        prefix = row["project_prefix"]
        if row["state"] == "closed" or not ring1.visible(db, prefix, principal):
            continue
        reference = {"project": prefix, "ticket": projects.row_reference(row)}
        if row["kind"] == "standing":
            if exploration_available(db, row, now):
                standing_goals.append(standing_summary(row, include_ticket=True))
        else:
            dated_goals.append({**reference, **dated_goal(db, prefix, owner, row)})
    return {"standing_goals": standing_goals, "dated_goals": dated_goals}


def save(db, project, owner, goal):
    row = next(iter(goal_rows(db, [owner], project, "original")), None)
    related = owned_tickets(db, project, owner, goal[2]) if goal is not None else None
    write_goal(db, project, owner, "original", row, goal)
    return {"summary": goal[0], "deadline": goal[1], "tickets": related} if goal is not None else None


def owned_tickets(db, project, owner, ids):
    related = tickets(db, project, owner, ids)
    if len(related) != len(ids):
        raise ValueError("Choose tickets you own in this project.")
    return related


def milestones(db, project, owner, *, include_ticket=False):
    return [
        {**dated_goal(db, project, owner, row),
         **({"ticket": projects.row_reference(row)} if include_ticket else {})}
        for row in goal_rows(db, [owner], project, "milestone")
    ]


def save_milestone(db, project, owner, previous, goal):
    rows = {row["summary"]: row for row in goal_rows(db, [owner], project, "milestone")}
    row = rows.get(previous)
    if goal is None:
        write_goal(db, project, owner, "milestone", row, None)
        return None
    summary, deadline, ids = goal
    related = owned_tickets(db, project, owner, ids)
    if summary != previous and summary in rows:
        if previous is None:
            raise ValueError("A milestone with this name already exists. Edit it or choose a different name.")
        raise ValueError("Choose a different milestone name.")
    if previous is not None and row is None:
        raise LookupError("Milestone not found in your goals.")
    write_goal(db, project, owner, "milestone", row, goal)
    return {"summary": summary, "deadline": deadline, "tickets": related}
