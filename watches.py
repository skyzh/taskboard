"""Local ticket subscriptions and explicit links to source identities."""

import json
from pathlib import Path

import tasks_storage


PROVIDERS = {"github": "GitHub", "jira": "Jira"}
LOCAL_FILE = "Local file"
SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS sources (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        source_key TEXT NOT NULL,
        target TEXT NOT NULL,
        digest TEXT,
        UNIQUE (kind, source_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_sources (
        task_id INTEGER NOT NULL REFERENCES tasks (id),
        source_id INTEGER NOT NULL REFERENCES sources (id),
        PRIMARY KEY (task_id, source_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_references (
        task_id INTEGER NOT NULL REFERENCES tasks (id),
        url TEXT NOT NULL,
        label TEXT NOT NULL,
        PRIMARY KEY (task_id, url)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_deliveries (
        source_id INTEGER NOT NULL REFERENCES sources (id),
        delivery_key TEXT NOT NULL,
        PRIMARY KEY (source_id, delivery_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_watches (
        task_id INTEGER NOT NULL REFERENCES tasks (id),
        watcher TEXT NOT NULL,
        after_event INTEGER NOT NULL,
        PRIMARY KEY (task_id, watcher)
    )
    """,
)


def source_label(source):
    if source["kind"] == "tasknote":
        return LOCAL_FILE
    return f"{PROVIDERS[source['kind']]} · {source['source_key']}"


def source_url(source):
    target = source["target"]
    return Path(target).as_uri() if source["kind"] == "tasknote" else target


def find_source(db, kind, key):
    return db.execute(
        "SELECT * FROM sources WHERE kind = ? AND source_key = ?", (kind, key)
    ).fetchone()


def link_source(db, task_id, kind, key, target):
    """Return the source ID when this task gains a link."""
    if kind not in PROVIDERS:
        raise ValueError("Choose a GitHub or Jira source.")
    source = find_source(db, kind, key)
    if source is None:
        source_id = tasks_storage.insert_id(
            db, "INSERT INTO sources (kind, source_key, target) VALUES (?, ?, ?)",
            (kind, key, target),
        )
    else:
        if source["target"] != target:
            raise ValueError(f"{source_label(source)} already points to a different target.")
        source_id = source["id"]
    attached = db.execute(
        "INSERT INTO task_sources (task_id, source_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
        (task_id, source_id),
    )
    return source_id if attached.rowcount else None


def link_reference(db, task_id, url, label):
    """Return the change to this task reference, if any."""
    previous = db.execute(
        "SELECT 1 FROM task_references WHERE task_id = ? AND url = ?", (task_id, url),
    ).fetchone()
    saved = db.execute(
        """
        INSERT INTO task_references (task_id, url, label) VALUES (?, ?, ?)
        ON CONFLICT (task_id, url) DO UPDATE SET label = excluded.label
        WHERE task_references.label <> excluded.label
        """,
        (task_id, url, label),
    )
    if saved.rowcount:
        return "relabel" if previous else "add"
    return None


def accept_delivery(db, source_id, key):
    inserted = db.execute(
        "INSERT INTO source_deliveries (source_id, delivery_key) VALUES (?, ?) ON CONFLICT DO NOTHING",
        (source_id, key),
    )
    return bool(inserted.rowcount)


def linked_tasks(db, source_id):
    return db.execute("SELECT task_id FROM task_sources WHERE source_id = ? ORDER BY task_id", (source_id,))


def has_table(db, name):
    return tasks_storage.has_table(db, name)


def linked_sources(db, task_id=None):
    if not has_table(db, "task_sources"):
        return ()
    query = "SELECT task_sources.task_id, sources.* FROM task_sources JOIN sources ON source_id = sources.id"
    ordering = " ORDER BY sources.kind, sources.source_key"
    if task_id is None:
        return db.execute(query + ordering)
    return db.execute(query + " WHERE task_sources.task_id = ?" + ordering, (task_id,))


def task_source_links(db, task_id=None):
    """Display explicit references and provider links, using task-specific labels for shared URLs."""
    references = {}
    if has_table(db, "task_references"):
        where, parameters = ("", ()) if task_id is None else (" WHERE task_id = ?", (task_id,))
        references = {
            (number, url): label
            for number, url, label in db.execute(
                "SELECT task_id, url, label FROM task_references" + where + " ORDER BY task_id, url",
                parameters,
            )
        }
    for source in linked_sources(db, task_id):
        number, url = source["task_id"], source_url(source)
        label = references.pop((number, url), None)
        yield number, label or source_label(source), url, label is None and source["kind"] == "github"
    for (number, url), label in references.items():
        yield number, label, url, False


def display_event(row):
    """Project retired local-source labels without changing stored history."""
    event = dict(row)
    links = json.loads(event["links"])
    if event["action"] == "source-event" and event["actor"] == "TaskNote":
        event["actor"] = LOCAL_FILE
        if event["note"] == "TaskNote file changed.":
            event["note"] = "Local file changed."
        if links and links[0][0].startswith("TaskNote · ") and links[0][1].startswith("file:"):
            links[0][0] = LOCAL_FILE
            event["links"] = json.dumps(links)
    elif (
        event["action"] == "log" and event["note"].startswith("Updated the path for TaskNote · ")
        and event["note"].endswith(".") and len(links) >= 2
        and links[0][0] == "Previous location" and links[1][0] == "Current TaskNote"
        and links[0][1].startswith("file:") and links[1][1].startswith("file:")
    ):
        event["note"] = "Local file path updated."
        links[1][0] = "Current local file"
        event["links"] = json.dumps(links)

    event.setdefault("context", "{}")
    context = json.loads(event["context"])
    audit = context.get("audit")
    if context.get("source_kind") == "tasknote" or (isinstance(audit, dict) and audit.get("source_kind") == "tasknote"):
        if context.get("source_kind") == "tasknote":
            context["source_kind"] = "local-file"
        if isinstance(audit, dict) and audit.get("source_kind") == "tasknote":
            audit["source_kind"] = "local-file"
        event["context"] = json.dumps(context)
    return event


def watch(db, task_id, watcher):
    result = db.execute(
        """
        INSERT INTO task_watches (task_id, watcher, after_event)
        SELECT ?, ?, COALESCE(MAX(id), 0) FROM events WHERE TRUE
        ON CONFLICT DO NOTHING
        """,
        (task_id, watcher),
    )
    return bool(result.rowcount)


def unwatch(db, task_id, watcher):
    result = db.execute(
        "DELETE FROM task_watches WHERE task_id = ? AND watcher = ?", (task_id, watcher)
    )
    return bool(result.rowcount)


def updates(db, watcher):
    if not has_table(db, "task_watches"):
        return ()
    return db.execute(
        """
        SELECT events.*, tasks.title
        FROM task_watches
        JOIN tasks ON tasks.id = task_watches.task_id
        JOIN events ON events.task_id = tasks.id AND events.id > task_watches.after_event
        WHERE task_watches.watcher = ?
        ORDER BY events.id DESC
        """,
        (watcher,),
    )
