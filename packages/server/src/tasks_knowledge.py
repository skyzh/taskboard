"""Organize maintained ticket briefs and reconcile explicit, source-backed changes."""

from argparse import Namespace

import tasks_core as ledger
import tasks_projects as projects
import watches


DECISION_TAG = "decision-record"
SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS knowledge_areas (
        project TEXT NOT NULL REFERENCES projects (prefix),
        path TEXT NOT NULL,
        index_id BIGINT NOT NULL UNIQUE REFERENCES tasks (id),
        PRIMARY KEY (project, path)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_entries (
        task_id BIGINT PRIMARY KEY REFERENCES tasks (id),
        project TEXT NOT NULL,
        path TEXT NOT NULL,
        FOREIGN KEY (project, path) REFERENCES knowledge_areas (project, path)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_stages (
        entry_id BIGINT NOT NULL REFERENCES tasks (id),
        source_id BIGINT NOT NULL REFERENCES tasks (id),
        base_brief TEXT NOT NULL,
        proposed_brief TEXT,
        source_event_id BIGINT REFERENCES events (id),
        staged_at TEXT NOT NULL,
        applied_event_id BIGINT REFERENCES events (id),
        conflict TEXT,
        PRIMARY KEY (entry_id, source_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS knowledge_stage_source ON knowledge_stages (source_id)",
    """
    CREATE TABLE IF NOT EXISTS knowledge_maintenance_assignments (
        task_id BIGINT PRIMARY KEY REFERENCES tasks (id),
        dri TEXT NOT NULL REFERENCES board_principals (ledger_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS knowledge_maintenance_dri ON knowledge_maintenance_assignments (dri, task_id)",
)


def migrate_records(db):
    """Recognize published legacy notes without rewriting their history or links."""
    current = ledger.current_tasks_sql(db, include_knowledge=True)
    db.execute(
        f"""
        WITH current AS ({current})
        UPDATE tasks SET record_kind = 'knowledge'
        WHERE id IN (
            SELECT current.id FROM current JOIN task_tags ON task_tags.task_id = current.id
            WHERE current.record_kind = 'work' AND current.display_state = 'done' AND task_tags.tag = ?
        )
        """, (DECISION_TAG,),
    )


def create(db, actor, project, title, brief):
    args = Namespace(
        project=project, title=ledger.task_title(title), brief=brief, by=actor, dri="",
        step="Knowledge entry created.",
    )
    # Keep the legacy completed-tagged representation readable after rollback.
    number = ledger.create_task_record(db, args, [], state="done", context={"record_kind": "knowledge"})
    db.execute("UPDATE tasks SET record_kind = 'knowledge' WHERE id = ?", (number,))
    db.execute("INSERT INTO task_tags (task_id, tag) VALUES (?, ?)", (number, DECISION_TAG))
    ledger.bump_revision(db)
    ticket = projects.for_task(db, number)
    return {"id": ticket, "record_kind": "knowledge", "receipt": f"{ticket} · Knowledge entry created."}


def is_maintained(db, task):
    return ledger.is_knowledge(task) or (
        task["display_state"] == "done" and DECISION_TAG in ledger.task_tags(db, task["id"])
    )


def maintained(db, number):
    task = ledger.get_task(db, number)
    if not is_maintained(db, task):
        raise ValueError("Choose a completed ticket tagged decision-record.")
    return task


def maintenance_assignments(db, numbers):
    if not numbers:
        return {}
    marks = ", ".join("?" for _ in numbers)
    return {row["task_id"]: {"id": row["dri"], "name": row["name"]} for row in db.execute(
        f"""SELECT assignment.task_id, assignment.dri,
                   COALESCE(NULLIF(principal.name, ''), NULLIF(principal.username, ''), assignment.dri) AS name
            FROM knowledge_maintenance_assignments AS assignment
            JOIN board_principals AS principal ON principal.ledger_key = assignment.dri
            WHERE assignment.task_id IN ({marks})""", numbers,
    )}


def set_maintenance_dri(db, note, actor, dri):
    number = note["id"]
    current = maintenance_assignments(db, (number,)).get(number)
    previous = current["id"] if current else None
    if previous == dri:
        return {"id": projects.row_reference(note), "maintenance_dri": current,
                "receipt": "Knowledge DRI unchanged."}
    if dri is None:
        db.execute("DELETE FROM knowledge_maintenance_assignments WHERE task_id = ?", (number,))
    else:
        db.execute(
            """INSERT INTO knowledge_maintenance_assignments (task_id, dri) VALUES (?, ?)
               ON CONFLICT (task_id) DO UPDATE SET dri = excluded.dri""", (number, dri),
        )
    current = maintenance_assignments(db, (number,)).get(number)
    message = f"Assigned Knowledge maintenance to {current['name']}." if current else "Cleared the Knowledge DRI."
    ledger.record(db, number, "log", actor, note["state"], message, None, [],
                  {"previous_knowledge_dri": previous, "knowledge_dri": dri})
    ledger.bump_revision(db)
    return {"id": projects.row_reference(note), "maintenance_dri": current, "receipt": message}


def place(db, number, actor, area, *, index=False):
    note = maintained(db, number)
    if not ledger.may_tag(db, note, actor):
        raise PermissionError("Only the Knowledge entry owner or project manager can organize it.")
    parts = [part.strip() for part in area.split("/")]
    if any(not part or part in (".", "..") or not part.isprintable() for part in parts):
        raise ValueError("Provide named areas separated by a slash, such as Storage performance/Latency.")
    project, path = projects.row_project(note), "/".join(parts)
    existing = db.execute(
        "SELECT index_id FROM knowledge_areas WHERE project = ? AND path = ?", (project, path),
    ).fetchone()
    index_area = db.execute("SELECT path FROM knowledge_areas WHERE index_id = ?", (number,)).fetchone()
    if index_area and index_area["path"] != path:
        raise ValueError("This entry is the index of another area.")
    if index and existing and existing["index_id"] != number:
        if is_maintained(db, ledger.get_task(db, existing["index_id"])):
            raise ValueError("This area already has an index entry.")
        if not projects.manages(db, project, actor):
            raise PermissionError("Only the project manager can replace an inactive area index.")
        db.execute("UPDATE knowledge_areas SET index_id = ? WHERE project = ? AND path = ?", (number, project, path))
    if existing is None:
        if not index:
            raise ValueError("Create the area's index before placing other entries in it.")
        if len(parts) > 1 and db.execute(
            "SELECT 1 FROM knowledge_areas WHERE project = ? AND path = ?", (project, "/".join(parts[:-1])),
        ).fetchone() is None:
            raise ValueError("Create the parent area's index first.")
        db.execute("INSERT INTO knowledge_areas VALUES (?, ?, ?)", (project, path, number))
    old = db.execute("SELECT project, path FROM knowledge_entries WHERE task_id = ?", (number,)).fetchone()
    assigned_index = index and (existing is None or existing["index_id"] != number)
    if not assigned_index and old is not None and (old["project"], old["path"]) == (project, path):
        return {"receipt": "Knowledge area unchanged.", "project": project, "area": path}
    db.execute(
        """INSERT INTO knowledge_entries VALUES (?, ?, ?)
           ON CONFLICT (task_id) DO UPDATE SET project = excluded.project, path = excluded.path""",
        (number, project, path),
    )
    purpose = "Set as index for" if assigned_index else "Placed in"
    ledger.record(db, number, "log", actor, note["state"], f"{purpose} Knowledge area {path}.", None, [])
    ledger.bump_revision(db)
    return {"receipt": "Knowledge area saved.", "project": project, "area": path}


def stage(db, number, source_id, actor, brief, source_event):
    note = maintained(db, number)
    if number == source_id:
        raise ValueError("Link the separate work ticket that could change the Knowledge entry.")
    source = ledger.get_work_task(db, source_id)
    if not ledger.may_tag(db, source, actor):
        raise PermissionError("Only the source ticket owner or project manager can stage its work.")
    if (brief is None) != (source_event is None):
        raise ValueError("Provide both the proposed brief and its exact source event, or stage the link alone.")
    if source_event is not None and db.execute(
        "SELECT 1 FROM events WHERE id = ? AND task_id = ?", (source_event, source_id),
    ).fetchone() is None:
        raise ValueError("Choose an Activity event on the source ticket.")
    old = db.execute(
        "SELECT * FROM knowledge_stages WHERE entry_id = ? AND source_id = ?", (number, source_id),
    ).fetchone()
    if old and (old["proposed_brief"], old["source_event_id"]) == (brief, source_event):
        return {"receipt": "Knowledge stage unchanged."}
    if old and old["applied_event_id"] is not None:
        raise ValueError("This ticket's staged change was applied. Link a new work ticket for its successor.")
    db.execute(
        """
        INSERT INTO knowledge_stages
            (entry_id, source_id, base_brief, proposed_brief, source_event_id, staged_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (entry_id, source_id) DO UPDATE SET base_brief = excluded.base_brief,
            proposed_brief = excluded.proposed_brief, source_event_id = excluded.source_event_id,
            staged_at = excluded.staged_at, conflict = NULL
        """,
        (number, source_id, old["base_brief"] if old and not old["conflict"] else note["brief"],
         brief, source_event, ledger.timestamp()),
    )
    ledger.record(db, source_id, "log", actor, source["state"],
                  f"Staged work for Knowledge entry {projects.row_reference(note)}.", None, [])
    ledger.bump_revision(db)
    return {"receipt": "Knowledge change staged."}


def directory(db, *, project=None):
    return [{
        "project": row["project"], "path": row["path"], "index": projects.row_reference(row),
        "title": row["title"], "available": bool(row["available"]),
    } for row in db.execute(
        f"""WITH current AS ({ledger.current_tasks_sql(db, include_knowledge=True)})
            SELECT areas.project, areas.path, current.*,
                (current.record_kind = 'knowledge' OR (current.display_state = 'done' AND EXISTS (
                    SELECT 1 FROM task_tags WHERE task_id = current.id AND tag = ?
                ))) AS available
            FROM knowledge_areas AS areas JOIN current ON current.id = areas.index_id
            WHERE (? IS NULL OR areas.project = ?)
            ORDER BY areas.project, areas.path""", (DECISION_TAG, project, project),
    )]


def links(db, number, *, include_source=False, source_project=None):
    where = "(stage.entry_id = ? OR stage.source_id = ?)" if include_source else "stage.entry_id = ?"
    args = (number, number) if include_source else (number,)
    if source_project is not None:
        where += " AND source.project_prefix = ?"
        args += (source_project,)
    records = db.execute(f"""
        SELECT stage.*, source.id, source.title AS source_title, source.project_prefix, source.project_number,
               source_event.at AS source_at, entry.title AS entry_title,
               entry.project_prefix AS entry_project, COALESCE(entry.project_number, entry.id) AS entry_number
        FROM knowledge_stages AS stage
        JOIN tasks AS source ON source.id = stage.source_id
        JOIN tasks AS entry ON entry.id = stage.entry_id
        LEFT JOIN events AS source_event ON source_event.id = stage.source_event_id
        WHERE {where} ORDER BY stage.staged_at DESC, stage.source_id DESC
    """, args).fetchall()
    return [{
        "source": projects.row_reference(row), "source_title": row["source_title"],
        "entry": f"{row['entry_project']}-{row['entry_number']}", "entry_title": row["entry_title"],
        "staged_at": row["staged_at"], "source_at": row["source_at"], "source_event_id": row["source_event_id"],
        "applied_event_id": row["applied_event_id"], "conflict": row["conflict"],
    } for row in records]


def metadata(db, number, *, source_project=None):
    area = db.execute(
        """SELECT entries.project, entries.path, areas.index_id FROM knowledge_entries AS entries
           JOIN knowledge_areas AS areas ON areas.project = entries.project AND areas.path = entries.path
           WHERE entries.task_id = ?""", (number,),
    ).fetchone()
    result = {"area": area["path"]} if area is not None else {}
    if area is not None and area["index_id"] == number:
        result["area_entries"] = [
            {"key": projects.row_reference(row), "title": row["title"]}
            for row in db.execute(
                ledger.current_tasks_sql(db, include_knowledge=True) + """
                WHERE tasks.id IN (SELECT task_id FROM knowledge_entries WHERE project = ? AND path = ?)
                  AND tasks.id <> ? AND (
                      tasks.record_kind = 'knowledge' OR tasks.id IN (SELECT task_id FROM task_tags WHERE tag = ?)
                  )
                ORDER BY tasks.title, tasks.id
                """, (area["project"], area["path"], number, DECISION_TAG),
            ) if ledger.is_knowledge(row) or row["display_state"] == "done"
        ]
    if staged := links(db, number, source_project=source_project):
        result["staged_work"] = staged
    return result


def reconcile(db, project, actor, base, *, dri=None):
    pending = db.execute(
        """
        SELECT stage.*, source.id, source.project_prefix, source.project_number,
            EXISTS (SELECT 1 FROM events AS later WHERE later.task_id = stage.source_id
                AND later.id > stage.source_event_id
                AND json_extract(later.context, '$.ownership_action') = 'reopen') AS source_reopened
        FROM knowledge_stages AS stage
        JOIN tasks ON tasks.id = stage.entry_id
        JOIN tasks AS source ON source.id = stage.source_id
        WHERE tasks.project_prefix = ? AND applied_event_id IS NULL
          AND (? IS NULL OR EXISTS (
              SELECT 1 FROM knowledge_maintenance_assignments AS assignment
              WHERE assignment.task_id = stage.entry_id AND assignment.dri = ?
          ))
        ORDER BY entry_id, source_id
        """, (project, dri, dri),
    ).fetchall()
    entries = {}
    for row in pending:
        entries.setdefault(row["entry_id"], []).append(row)

    applied, unresolved = [], []
    for number, changes in entries.items():
        note = ledger.get_task(db, number)
        if not ledger.may_tag(db, note, actor):
            continue
        entry = projects.row_reference(note)
        contact = actor if ledger.is_knowledge(note) else note["dri"]
        settled = {row["source_id"]: row for row in changes if row["proposed_brief"] is not None
                   and ledger.get_task(db, row["source_id"])["display_state"] == "done"}
        current = note["brief"]
        candidates = {row["proposed_brief"] for row in settled.values()
                      if not row["source_reopened"] and row["base_brief"] == current}
        keep_entry = is_maintained(db, note)
        for row in changes:
            item = {"entry": entry, "source": projects.row_reference(row), "owner": contact}
            if row["source_id"] not in settled:
                unresolved.append({**item, "reason": "Awaiting a settled source and proposed brief."})
                continue
            proposal = row["proposed_brief"]
            conflict = None
            if not keep_entry:
                conflict = "The entry is no longer a maintained decision."
            elif row["source_reopened"]:
                conflict = "The source reopened after this evidence. Stage its current conclusion."
            elif proposal != current and row["base_brief"] != current:
                conflict = "The current brief changed after staging."
            elif proposal != current and len(candidates) > 1:
                conflict = "Settled sources propose different replacements."
            if conflict:
                unresolved.append({**item, "reason": conflict})
                if row["conflict"] != conflict:
                    db.execute("UPDATE knowledge_stages SET conflict = ? WHERE entry_id = ? AND source_id = ?",
                               (conflict, number, row["source_id"]))
                    ledger.bump_revision(db)
                continue
            source_url = f"{base}/#event-{row['source_event_id']}-log"
            evidence = [("Settled Knowledge source", source_url)]
            ledger.edit_task_details(db, Namespace(
                id=number, by=actor, title=None, brief=proposal, note="Applied a settled Knowledge correction.",
            ), evidence)
            if db.execute("SELECT 1 FROM task_references WHERE task_id = ? AND url = ?",
                          (number, source_url)).fetchone() is None:
                watches.link_reference(db, number, source_url, evidence[0][0])
            event = ledger.record(db, number, "log", actor, note["state"], "Settled Knowledge change applied.",
                                  None, evidence, {"knowledge_source_event": row["source_event_id"]})
            db.execute(
                """UPDATE knowledge_stages SET applied_event_id = ?, conflict = NULL
                   WHERE entry_id = ? AND source_id = ?""",
                (event, number, row["source_id"]),
            )
            ledger.bump_revision(db)
            applied.append({"entry": entry, "event_id": event, "source_event_id": row["source_event_id"]})
    return {"receipt": f"Applied {len(applied)} Knowledge changes.", "applied": applied, "unresolved": unresolved}
