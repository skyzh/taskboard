"""Explicit public snapshots of signed Knowledge entries."""

from io import BytesIO
import json
import re
from uuid import uuid4

from flask import g, request, send_file
from werkzeug.exceptions import NotFound

import tasks_audit as audit
import tasks_core as ledger
import tasks_projects as projects
import tasks_ring1 as ring1
import tasks_storage


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS knowledge_publications (
        task_id BIGINT PRIMARY KEY REFERENCES tasks (id),
        project TEXT NOT NULL REFERENCES projects (prefix),
        public_id TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        published_at TEXT NOT NULL,
        active INTEGER NOT NULL CHECK (active IN (0, 1))
    )""",
    """CREATE TABLE IF NOT EXISTS knowledge_publication_files (
        public_file_id TEXT PRIMARY KEY,
        publication_id TEXT NOT NULL REFERENCES knowledge_publications (public_id),
        attachment_id TEXT NOT NULL,
        UNIQUE (publication_id, attachment_id)
    )""",
    """CREATE INDEX IF NOT EXISTS knowledge_publication_order
        ON knowledge_publications (active, published_at, public_id)""",
)

OPAQUE_ID = re.compile(r"[0-9a-f]{32}")
PUBLIC_ENDPOINTS = frozenset({"public_knowledge_index", "public_knowledge_entry", "public_knowledge_download"})
VISIBLE = """
    FROM knowledge_publications AS publication
    JOIN tasks ON tasks.id = publication.task_id AND tasks.project_prefix = publication.project
    WHERE publication.active = 1 AND tasks.record_kind = 'knowledge'
      AND NOT EXISTS (SELECT 1 FROM project_ring1 WHERE project = publication.project)
"""


def register_routes(app, *, db, current_actor, task_number, can_edit, json_body, text, bad_request,
                    storage, base):
    api = "/api/v1/public/knowledge"

    def metadata(row):
        public_id = row["public_id"]
        return {
            "id": public_id, "title": row["title"], "excerpt": row["body"][:240],
            "published_at": row["published_at"], "url": f"{base}/public/knowledge/{public_id}",
        }

    def row_for(connection, public_id):
        if not OPAQUE_ID.fullmatch(public_id):
            raise NotFound()
        row = connection.execute(
            "SELECT publication.* " + VISIBLE + " AND publication.public_id = ?", (public_id,),
        ).fetchone()
        if row is None:
            raise NotFound()
        return row

    def granted_files(connection, row, public_file_id=None):
        if storage is None:
            return []
        query = """
            SELECT approval.public_file_id, files.id AS attachment_id, files.filename,
                   files.size_bytes, files.content_type
            FROM knowledge_publication_files AS approval
            JOIN ticket_attachments AS files ON files.id = approval.attachment_id
            JOIN tasks AS source ON source.id = files.task_id AND source.project_prefix = ?
            WHERE approval.publication_id = ? AND files.backend = ?
        """
        params = (row["project"], row["public_id"], storage.kind)
        if public_file_id is not None:
            query += " AND approval.public_file_id = ?"
            params += (public_file_id,)
        return connection.execute(query + " ORDER BY approval.public_file_id", params).fetchall()

    def editor(connection, identifier, actor):
        number = task_number(identifier)
        task = ledger.find_task(connection, number)
        if task is None or not ledger.is_knowledge(task):
            raise NotFound()
        project = projects.row_project(task)
        if ring1.private(connection, project):
            raise NotFound()
        if not can_edit(connection, actor, task, project):
            raise bad_request("This Knowledge entry requires project write access.", 403)
        return number, project

    def selected_files(connection, ids, project, actor):
        if not isinstance(ids, list) or len(ids) > 50 or any(
            not isinstance(item, str) or not OPAQUE_ID.fullmatch(item) for item in ids
        ) or len(set(ids)) != len(ids):
            raise bad_request("Choose up to 50 distinct attachment IDs.")
        if ids and storage is None:
            raise NotFound()
        for key in ids:
            row = connection.execute(
                "SELECT task_id FROM ticket_attachments WHERE id = ? AND backend = ?", (key, storage.kind),
            ).fetchone()
            source = ledger.find_task(connection, row["task_id"]) if row else None
            if source is None or projects.row_project(source) != project or not can_edit(
                connection, actor, source, project,
            ):
                raise NotFound()
        return set(ids)

    @app.after_request
    def public_knowledge_cache(response):
        if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint == "knowledge_publication":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get(api)
    def public_knowledge_index():
        q = text(request.args, "q", optional=True, maximum=160)
        limit = request.args.get("limit", "20")
        if not re.fullmatch(r"[1-9][0-9]?", limit) or int(limit) > 50:
            raise bad_request("Choose a page size from 1 to 50.")
        limit = int(limit)
        where, params = "", ()
        if q:
            search = "%" + q.lower().replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
            where += " AND (LOWER(publication.title) LIKE ? ESCAPE '!' OR LOWER(publication.body) LIKE ? ESCAPE '!')"
            params += (search, search)
        cursor = text(request.args, "before", optional=True, maximum=120)
        if cursor is not None:
            try:
                at, public_id = json.loads(cursor)
                if not isinstance(at, str) or not isinstance(public_id, str) or not OPAQUE_ID.fullmatch(public_id):
                    raise ValueError()
            except (TypeError, ValueError) as error:
                raise bad_request("Use the cursor returned by the published Knowledge list.") from error
            where += """ AND (publication.published_at < ?
                            OR (publication.published_at = ? AND publication.public_id < ?))"""
            params += (at, at, public_id)
        with tasks_storage.read(db()) as connection:
            rows = connection.execute(
                "SELECT publication.* " + VISIBLE + where
                + " ORDER BY publication.published_at DESC, publication.public_id DESC LIMIT ?", (*params, limit + 1),
            ).fetchall()
        more = json.dumps([rows[limit - 1]["published_at"], rows[limit - 1]["public_id"]]) if len(rows) > limit else None
        return {"items": [metadata(row) for row in rows[:limit]], "next_cursor": more}

    @app.get(api + "/<public_id>")
    def public_knowledge_entry(public_id):
        with tasks_storage.read(db()) as connection:
            row = row_for(connection, public_id)
            attachments = [{
                "id": grant["public_file_id"], "filename": grant["filename"],
                "size_bytes": grant["size_bytes"], "content_type": grant["content_type"],
                "download_url": f"{base}{api}/{public_id}/attachments/{grant['public_file_id']}",
            } for grant in granted_files(connection, row)]
            return {**metadata(row), "body": row["body"], "attachments": attachments}

    @app.get(api + "/<public_id>/attachments/<public_file_id>")
    def public_knowledge_download(public_id, public_file_id):
        if not OPAQUE_ID.fullmatch(public_file_id):
            raise NotFound()
        with tasks_storage.read(db()) as connection:
            row = row_for(connection, public_id)
            grants = granted_files(connection, row, public_file_id)
            if not grants:
                raise NotFound()
            grant = grants[0]
            try:
                contents = storage.read(connection, grant["attachment_id"])
            except FileNotFoundError as error:
                raise NotFound() from error
        response = send_file(BytesIO(contents), mimetype="application/octet-stream",
                             as_attachment=True, download_name=grant["filename"])
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.route("/api/v1/knowledge/<identifier>/publication", methods=["GET", "PUT", "DELETE"])
    def knowledge_publication(identifier):
        write = request.method in ("PUT", "DELETE")
        actor = current_actor(required=True, write=write)
        if g.get("personal_agent_api") == "tailnet":
            raise bad_request("Sign in as a verified Knowledge editor.", 403)
        with (tasks_storage.write if write else tasks_storage.read)(db()) as connection:
            number, project = editor(connection, identifier, actor)
            row = connection.execute("SELECT * FROM knowledge_publications WHERE task_id = ?", (number,)).fetchone()
            if not write:
                if source_id := request.args.get("attachment_source"):
                    source = ledger.find_task(connection, task_number(source_id))
                    if (source is None or projects.row_project(source) != project
                            or not can_edit(connection, actor, source, project)):
                        raise NotFound()
                    files = connection.execute(
                        """SELECT id, filename FROM ticket_attachments
                           WHERE task_id = ? AND backend = ? ORDER BY filename, id""",
                        (source["id"], storage.kind),
                    ) if storage is not None else ()
                    return {"attachments": [dict(file) for file in files]}
                if row is None or row["active"] != 1 or row["project"] != project:
                    return {"publication": None}
                files = granted_files(connection, row)
                return {"publication": {
                    **metadata(row), "body": row["body"],
                    "attachment_ids": [grant["attachment_id"] for grant in files],
                    "attachments": [{"id": grant["attachment_id"], "filename": grant["filename"]} for grant in files],
                }}
            if request.method == "DELETE":
                if row is None or row["active"] != 1:
                    return {"publication": None}
                public_id = row["public_id"]
                connection.execute("DELETE FROM knowledge_publication_files WHERE publication_id = ?", (public_id,))
                connection.execute("UPDATE knowledge_publications SET active = 0 WHERE task_id = ?", (number,))
                operation = "unpublish"
                result = None
            else:
                data = json_body({"title", "body", "attachment_ids"})
                title = text(data, "title", maximum=240)
                body = text(data, "body", maximum=40000)
                ids = selected_files(connection, data.get("attachment_ids", []), project, actor)
                public_id = row["public_id"] if row else uuid4().hex
                existing = {grant["attachment_id"] for grant in connection.execute(
                    "SELECT attachment_id FROM knowledge_publication_files WHERE publication_id = ?", (public_id,),
                )}
                if row and (row["active"], row["project"], row["title"], row["body"], existing) == (
                    1, project, title, body, ids,
                ):
                    return {"publication": {**metadata(row), "body": body, "attachment_ids": sorted(ids)}}
                now = ledger.timestamp()
                connection.execute(
                    """INSERT INTO knowledge_publications (task_id, project, public_id, title, body, published_at, active)
                       VALUES (?, ?, ?, ?, ?, ?, 1) ON CONFLICT (task_id) DO UPDATE SET
                       project = excluded.project, title = excluded.title, body = excluded.body,
                       published_at = excluded.published_at, active = 1""",
                    (number, project, public_id, title, body, now),
                )
                for key in sorted(existing - ids):
                    connection.execute(
                        "DELETE FROM knowledge_publication_files WHERE publication_id = ? AND attachment_id = ?",
                        (public_id, key),
                    )
                for key in sorted(ids - existing):
                    connection.execute("INSERT INTO knowledge_publication_files VALUES (?, ?, ?)",
                                       (uuid4().hex, public_id, key))
                operation = "publish"
                result = {**metadata({"public_id": public_id, "title": title, "body": body, "published_at": now}),
                          "body": body, "attachment_ids": sorted(ids)}
            ledger.bump_revision(connection, project)
            audit.record(connection, scope="private", actor=actor["ledger_key"], operation=operation,
                         record_type="knowledge_publication", record_id=public_id, at=ledger.timestamp())
            return {"publication": result}
