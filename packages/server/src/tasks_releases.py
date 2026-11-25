"""Operator receipts and the release queue projection for the Taskboard project."""

from datetime import datetime, timedelta
import json
import re
from urllib.parse import urlsplit

import tasks_merges as merges
import tasks_projects as projects
import tasks_storage
from tasks_release_changelog import normalize_changelog


ACTIVE_STATES = ("queued", "running")
FINAL_STATES = ("released", "rejected", "rolled_back", "rollback_failed")
DEFAULT_QUEUE_TICKET = "T-78"
SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS release_batches (
        id INTEGER PRIMARY KEY,
        release_id TEXT NOT NULL UNIQUE,
        candidate TEXT NOT NULL,
        release_number INTEGER UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS release_events (
        batch_id INTEGER NOT NULL REFERENCES release_batches (id),
        status TEXT NOT NULL,
        active TEXT,
        at TEXT NOT NULL,
        receipt_url TEXT,
        activity_task_id INTEGER REFERENCES tasks (id),
        PRIMARY KEY (batch_id, status)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS release_tasks (
        batch_id INTEGER NOT NULL REFERENCES release_batches (id),
        task_id INTEGER NOT NULL REFERENCES tasks (id),
        PRIMARY KEY (batch_id, task_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS release_tasks_by_task ON release_tasks (task_id, batch_id)",
    """
    CREATE TABLE IF NOT EXISTS release_changelogs (
        batch_id INTEGER PRIMARY KEY REFERENCES release_batches (id),
        data TEXT NOT NULL
    )
    """,
)

_CURRENT = """
    SELECT release_batches.*, outcome.status, outcome.active, outcome.at, outcome.receipt_url
    FROM release_batches
    JOIN release_events AS outcome ON outcome.batch_id = release_batches.id AND outcome.status = (
        SELECT candidate_event.status FROM release_events AS candidate_event
        WHERE candidate_event.batch_id = release_batches.id
        ORDER BY CASE candidate_event.status WHEN 'queued' THEN 0 WHEN 'running' THEN 1 ELSE 2 END DESC
        LIMIT 1
    )
"""


def ensure_schema(db):
    for sql in SCHEMA:
        db.execute(sql)
    columns = tasks_storage.table_columns(db, "release_events")
    if "activity_task_id" not in columns:
        db.execute("ALTER TABLE release_events ADD COLUMN activity_task_id INTEGER REFERENCES tasks (id)")


def normalize_receipt(data):
    required = {"release_id", "ticket", "candidate", "status", "active", "at"}
    if not isinstance(data, dict) or not required <= data.keys() or data.keys() - (required | {"receipt_url", "changelog"}):
        raise ValueError("Use the documented release receipt fields.")
    release_id, ticket = data["release_id"], data["ticket"]
    if not isinstance(release_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", release_id):
        raise ValueError("Use a stable release ID.")
    if not isinstance(ticket, str) or not re.fullmatch(r"T-[1-9][0-9]{0,18}", ticket):
        raise ValueError("Release receipts require a Taskboard ticket such as T-1.")
    projects.identifier(ticket)
    candidate, active, status = data["candidate"], data["active"], data["status"]
    for digest in (candidate, active) if active is not None else (candidate,):
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Use full SHA-256 release digests.")
    if status not in (*ACTIVE_STATES, *FINAL_STATES):
        raise ValueError("Use a supported release status.")
    if status == "released" and active != candidate:
        raise ValueError("A successful release must activate the exact candidate.")
    if status == "rolled_back" and active in (None, candidate):
        raise ValueError("A rollback must identify the restored release.")
    at = data["at"]
    if not isinstance(at, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)", at,
    ):
        raise ValueError("Use an explicit ISO UTC timestamp for the release event.")
    moment = datetime.fromisoformat(at)
    if moment.utcoffset() != timedelta(0):
        raise ValueError("Use an explicit ISO UTC timestamp for the release event.")
    receipt_url = data.get("receipt_url")
    if receipt_url is not None:
        if not isinstance(receipt_url, str) or not re.fullmatch(r"https://\S{1,1992}", receipt_url):
            raise ValueError("Use an HTTPS evidence link.")
        link = urlsplit(receipt_url)
        if not link.hostname or link.username is not None or link.password is not None:
            raise ValueError("Use an HTTPS evidence link without credentials.")
    changelog = None
    if "changelog" in data:
        if status != "released":
            raise ValueError("Only a successful release receipt can publish a changelog.")
        changelog = normalize_changelog(data["changelog"])
    at = moment.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return release_id, ticket, candidate, status, active, at, receipt_url, changelog


def activity_task(db, receipt):
    if receipt["activity_task_id"] is not None:
        return receipt["activity_task_id"]
    # Older receipts do not identify which related ticket recorded the outcome.
    return db.execute(
        "SELECT CASE WHEN COUNT(*) = 1 THEN MIN(task_id) END FROM release_tasks WHERE batch_id = ?",
        (receipt["batch_id"],),
    ).fetchone()[0]


def matches_activity_outcome(details, ticket, receipt):
    if not details:
        return False
    try:
        result = json.loads(details)
        if (not isinstance(result, dict) or result.get("schema") != "taskboard-release-result/v1"
                or result.get("ticket") != ticket):
            return False
        fields = ("release_id", "ticket", "candidate", "status", "active", "at")
        release_id, _, candidate, status, active, at, _, _ = normalize_receipt(
            {key: result[key] for key in fields},
        )
    except (KeyError, TypeError, ValueError):
        return False
    return (release_id, candidate, status, active, at) == tuple(
        receipt[key] for key in ("release_id", "candidate", "status", "active", "at")
    )


def task_for_release(db, ticket, release_id, status):
    task_id = projects.resolve(db, ticket)
    task = db.execute("SELECT project_prefix FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is not None:
        if projects.release_queue_enabled(task["project_prefix"]):
            return task_id
        replay = db.execute(
            """SELECT 1 FROM release_batches AS batch
               JOIN release_events AS event ON event.batch_id = batch.id
               JOIN release_tasks AS member ON member.batch_id = batch.id
               WHERE batch.release_id = ? AND event.status = ? AND member.task_id = ?""",
            (release_id, status, task_id),
        ).fetchone()
        if replay is not None:
            return task_id
    raise ValueError("Choose an existing Taskboard ticket.")


def record_changelog(db, batch_id, changelog):
    data = json.dumps(changelog, sort_keys=True, ensure_ascii=False)
    existing = db.execute("SELECT data FROM release_changelogs WHERE batch_id = ?", (batch_id,)).fetchone()
    if existing is not None:
        if existing["data"] != data:
            raise ValueError("This batch already records a different changelog.")
        return
    for entry in changelog["entries"]:
        task_id = projects.resolve(db, entry["ticket"])
        receipt_id = entry["merge_receipt_id"]
        if merges.recorded_merges(db, task_id, [receipt_id]).get(receipt_id) != entry["merge_commit"]:
            raise ValueError("Each changelog entry must match the recorded merge on that ticket.")
        db.execute("INSERT INTO release_tasks VALUES (?, ?) ON CONFLICT DO NOTHING", (batch_id, task_id))
    db.execute("INSERT INTO release_changelogs VALUES (?, ?)", (batch_id, data))


def record_receipt(db, data):
    """Append one existing operator receipt inside the caller's transaction."""
    release_id, ticket, candidate, status, active, at, receipt_url, changelog = normalize_receipt(data)
    task_id = task_for_release(db, ticket, release_id, status)

    db.execute("INSERT INTO release_batches (release_id, candidate) VALUES (?, ?) ON CONFLICT DO NOTHING", (release_id, candidate))
    batch = db.execute(
        "SELECT id, candidate, release_number FROM release_batches WHERE release_id = ?", (release_id,),
    ).fetchone()
    if batch["candidate"] != candidate:
        raise ValueError("Use a new release ID for a different exact candidate.")
    batch_id = batch["id"]
    existing = db.execute("SELECT status, active, at, receipt_url FROM release_events WHERE batch_id = ?", (batch_id,)).fetchall()
    for event in existing:
        if event["status"] == status and (event["active"], event["at"], event["receipt_url"]) != (active, at, receipt_url):
            raise ValueError("This batch already records a different receipt for that status.")
        if status in FINAL_STATES and event["status"] in FINAL_STATES and event["status"] != status:
            raise ValueError("This batch already has a different outcome. Requeue under a new release ID.")
    db.execute(
        "INSERT INTO release_events (batch_id, status, active, at, receipt_url, activity_task_id) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (batch_id, status, active, at, receipt_url, task_id),
    )
    db.execute("INSERT INTO release_tasks VALUES (?, ?) ON CONFLICT DO NOTHING", (batch_id, task_id))
    if changelog is not None:
        record_changelog(db, batch_id, changelog)
    number = batch["release_number"]
    if status == "released" and number is None:
        number = db.execute("SELECT COALESCE(MAX(release_number), 0) + 1 FROM release_batches").fetchone()[0]
        db.execute("UPDATE release_batches SET release_number = ? WHERE id = ?", (number, batch_id))
    return {"release_id": release_id, "ticket": ticket, "status": status, "release_number": number}


def ticket_fields(db, task_ids):
    if not task_ids:
        return {}
    marks = ", ".join("?" for _ in task_ids)
    rows = db.execute(
        f"""
        SELECT release_tasks.task_id,
               MAX(CASE WHEN current.status IN ('queued', 'running') THEN 1 ELSE 0 END) AS queued_for_release,
               MAX(current.release_number) AS release_number
        FROM release_tasks JOIN ({_CURRENT}) AS current ON current.id = release_tasks.batch_id
        WHERE release_tasks.task_id IN ({marks}) GROUP BY release_tasks.task_id
        """,
        task_ids,
    )
    fields = {task_id: {"queued_for_release": False, "release_number": None} for task_id in task_ids}
    for row in rows:
        fields[row["task_id"]] = {
            "queued_for_release": bool(row["queued_for_release"]), "release_number": row["release_number"],
        }
    return fields


def active_release(db):
    """Read the latest operator receipt that establishes or invalidates live state."""
    row = db.execute(
        """
        SELECT release_batches.release_id, release_batches.release_number,
               outcome.status, outcome.at, outcome.receipt_url,
               CASE WHEN outcome.status <> 'rollback_failed' THEN outcome.active END AS active
        FROM release_events AS outcome
        JOIN release_batches ON release_batches.id = outcome.batch_id
        WHERE outcome.status IN ('released', 'rolled_back', 'rollback_failed')
        ORDER BY outcome.at DESC, release_batches.id DESC
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


def ticket_results(db, task_ids):
    """Project the latest terminal receipt explicitly linked to each ticket."""
    if not task_ids:
        return {}
    marks = ", ".join("?" for _ in task_ids)
    states = ", ".join("?" for _ in FINAL_STATES)
    rows = db.execute(
        f"""
        WITH ranked AS (
            SELECT release_tasks.task_id, outcome.status, outcome.at, outcome.receipt_url,
                   batch.release_number,
                   ROW_NUMBER() OVER (
                       PARTITION BY release_tasks.task_id ORDER BY outcome.at DESC, batch.id DESC
                   ) AS recent
            FROM release_tasks
            JOIN release_events AS outcome USING (batch_id)
            JOIN release_batches AS batch ON batch.id = release_tasks.batch_id
            WHERE release_tasks.task_id IN ({marks}) AND outcome.status IN ({states})
        )
        SELECT task_id, status, at, receipt_url, release_number FROM ranked WHERE recent = 1
        """,
        (*task_ids, *FINAL_STATES),
    )
    return {
        row["task_id"]: {field: row[field] for field in ("status", "at", "receipt_url", "release_number")}
        for row in rows
    }


def queue(db, *, before, limit):
    rows = db.execute(
        f"""
        SELECT current.*, release_changelogs.data AS changelog FROM ({_CURRENT}) AS current
        LEFT JOIN release_changelogs ON release_changelogs.batch_id = current.id
        WHERE current.id < ? ORDER BY current.id DESC LIMIT ?
        """,
        (before or projects.MAX_ID, limit + 1),
    ).fetchall()
    visible = rows[:limit]
    members = {row["id"]: [] for row in visible}
    if members:
        marks = ", ".join("?" for _ in members)
        for task in db.execute(
            f"""
            SELECT tasks.*, release_tasks.batch_id FROM release_tasks
            JOIN tasks ON tasks.id = release_tasks.task_id
            WHERE release_tasks.batch_id IN ({marks}) ORDER BY tasks.id
            """,
            tuple(members),
        ):
            members[task["batch_id"]].append({
                "id": task["id"], "key": projects.row_reference(task), "title": task["title"],
            })
    return {
        "items": [
            {**{key: row[key] for key in ("release_id", "candidate", "status", "active", "at", "receipt_url",
                                         "release_number")}, "tickets": members[row["id"]],
             "changelog": published_changelog(row["changelog"])}
            for row in visible
        ],
        "next_cursor": visible[-1]["id"] if len(rows) > limit else None,
    }


def published_changelog(data):
    if data is None:
        return None
    changelog = json.loads(data)
    for entry in changelog["entries"]:
        entry["merge_receipt_url"] = f"#event-{entry['merge_receipt_id']}-log"
    return changelog
