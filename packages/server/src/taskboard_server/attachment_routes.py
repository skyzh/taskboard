"""Ticket-scoped attachment routes using existing board identity and visibility."""

from base64 import b64decode, b64encode
from binascii import Error as Base64Error
from contextlib import closing
from hashlib import sha256
from io import BytesIO
import re
from uuid import UUID, uuid4

from flask import request, send_file
from werkzeug.exceptions import NotFound

import tasks_core as ledger
import tasks_projects as projects
import tasks_storage
from tasks_attachments import MAX_UPLOAD_BYTES


AGENT_UPLOAD_BYTES = 90 * 1024
AGENT_CHUNK_BYTES = 32 * 1024
ACTIONS = frozenset({"list-attachments", "upload-attachment", "read-attachment-chunk", "remove-attachment"})


MEDIA_TYPE = re.compile(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+")
FIELDS = "id, filename, content_type, size_bytes, created_by, created_at"


class AttachmentError(ValueError):
    def __init__(self, message, status, code):
        super().__init__(message)
        self.status = status
        self.code = code


def storage_unavailable():
    return AttachmentError("Attachment storage is unavailable.", 503, "attachment_storage_unavailable")


def check_upload(upload_id, name, content_type, contents, limit):
    try:
        upload_id = UUID(upload_id.strip()).hex
    except (AttributeError, ValueError) as error:
        raise AttachmentError("Supply a UUID upload ID.", 400, "invalid_attachment") from error
    name = name.strip() if isinstance(name, str) else ""
    if (name in ("", ".", "..") or not name.isprintable()
            or any(char in name for char in "/\\:") or len(name.encode("utf-8")) > 255):
        raise AttachmentError("Choose a file with a safe filename.", 400, "invalid_attachment")
    content_type = content_type.lower() if isinstance(content_type, str) else ""
    if len(content_type) > 127 or not MEDIA_TYPE.fullmatch(content_type):
        raise AttachmentError("Supply a valid content type.", 400, "invalid_attachment")
    if len(contents) > limit:
        raise AttachmentError("The attachment exceeds the upload limit.", 413, "attachment_too_large")
    return upload_id, name, content_type, contents


def browser_upload():
    if set(request.files) != {"file"} or set(request.form) != {"upload_id"} or (
        len(request.files.getlist("file")) != 1 or len(request.form.getlist("upload_id")) != 1
    ):
        raise AttachmentError("Supply one file and one upload ID.", 400, "invalid_attachment")
    with closing(request.files["file"]) as file:
        return check_upload(request.form["upload_id"], file.filename, file.mimetype or "application/octet-stream",
                            file.stream.read(MAX_UPLOAD_BYTES + 1), MAX_UPLOAD_BYTES)


def agent_upload(data):
    encoded = data["content_base64"]
    if not isinstance(encoded, str):
        raise AttachmentError("Supply Base64 attachment bytes.", 400, "invalid_attachment")
    if len(encoded) > AGENT_UPLOAD_BYTES * 4 // 3:
        raise AttachmentError("The attachment exceeds the agent upload limit.", 413, "attachment_too_large")
    try:
        contents = b64decode(encoded, validate=True)
    except (Base64Error, ValueError) as error:
        raise AttachmentError("Supply Base64 attachment bytes.", 400, "invalid_attachment") from error
    return check_upload(data["upload_id"], data["filename"], data["content_type"], contents, AGENT_UPLOAD_BYTES)


def register_routes(app, *, db, current_actor, task_number, can_edit, storage, mount):
    @app.errorhandler(AttachmentError)
    def attachment_error(error):
        return {"error": str(error), "code": error.code}, error.status

    def resolve(connection, reference, actor, *, write):
        number = task_number(reference)
        item = ledger.find_task(connection, number)
        if item is None or ledger.is_knowledge(item):
            raise NotFound()
        project = projects.row_project(item)
        editable = can_edit(connection, actor, item, project)
        if write and not editable:
            raise AttachmentError("This ticket requires project write access.", 403, "attachment_edit_denied")
        if storage is None or (storage.kind == "filesystem" and not storage.root.is_dir()):
            raise storage_unavailable()
        return number, project, editable

    def metadata(row, reference):
        result = {field: row[field] for field in FIELDS.split(", ")}
        return {**result, "download_url": f"{mount}/api/v1/tasks/{reference}/attachments/{row['id']}/download"}

    def selected(connection, number, key):
        row = connection.execute(
            f"SELECT {FIELDS}, backend FROM ticket_attachments WHERE task_id = ? AND id = ?", (number, key)
        ).fetchone()
        if row is None:
            raise NotFound()
        if row["backend"] != storage.kind:
            raise storage_unavailable()
        return row

    def list_attachments(reference, actor, limit):
        with tasks_storage.read(db()) as connection:
            number, _, editable = resolve(connection, reference, actor, write=False)
            rows = connection.execute(
                f"SELECT {FIELDS}, backend FROM ticket_attachments WHERE task_id = ? ORDER BY created_at, id", (number,)
            ).fetchall()
            if any(row["backend"] != storage.kind for row in rows):
                raise storage_unavailable()
            return {"attachments": [metadata(row, reference) for row in rows],
                    "max_upload_bytes": limit, "can_edit": editable}

    def upload_attachment(reference, actor, read_upload):
        with tasks_storage.write(db()) as connection:
            number, project, _ = resolve(connection, reference, actor, write=True)
            upload_id, name, content_type, contents = read_upload()
            digest = sha256(contents).hexdigest()
            existing = connection.execute(
                f"SELECT {FIELDS}, sha256, backend FROM ticket_attachments WHERE task_id = ? AND upload_id = ?",
                (number, upload_id),
            ).fetchone()
            if existing is not None:
                if (existing["filename"], existing["content_type"], existing["sha256"]) != (name, content_type, digest):
                    raise AttachmentError("This upload ID is already bound to another file.", 409, "upload_id_conflict")
                if existing["backend"] != storage.kind:
                    raise storage_unavailable()
                return {"attachment": metadata(existing, reference)}, 200

            row = {"id": uuid4().hex, "filename": name, "content_type": content_type, "size_bytes": len(contents),
                   "created_by": actor["ledger_key"], "created_at": ledger.timestamp()}
            connection.execute(
                f"""INSERT INTO ticket_attachments ({FIELDS}, task_id, upload_id, sha256, backend)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*(row[field] for field in FIELDS.split(", ")), number, upload_id, digest, storage.kind),
            )
            ledger.bump_revision(connection, project)
            try:
                storage.write(connection, row["id"], contents)
            except OSError as error:
                raise storage_unavailable() from error
        return {"attachment": metadata(row, reference)}, 201

    def read_attachment(reference, key, actor):
        with tasks_storage.read(db()) as connection:
            number, _, _ = resolve(connection, reference, actor, write=False)
            row = selected(connection, number, key)
            try:
                contents = storage.read(connection, row["id"])
            except OSError as error:
                raise storage_unavailable() from error
            return row, contents

    @app.get("/api/v1/tasks/<reference>/attachments")
    def list_ticket_attachments(reference):
        return list_attachments(reference, current_actor(required=True), MAX_UPLOAD_BYTES)

    @app.post("/api/v1/tasks/<reference>/attachments")
    def upload_ticket_attachment(reference):
        return upload_attachment(reference, current_actor(required=True, write=True), browser_upload)

    @app.get("/api/v1/tasks/<reference>/attachments/<key>/download")
    def download_ticket_attachment(reference, key):
        row, contents = read_attachment(reference, key, current_actor(required=True))
        response = send_file(BytesIO(contents), mimetype="application/octet-stream",
                             as_attachment=True, download_name=row["filename"])
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def remove_attachment(reference, key, actor):
        with tasks_storage.write(db()) as connection:
            number, project, _ = resolve(connection, reference, actor, write=True)
            row = selected(connection, number, key)
            if storage.kind == "postgres":
                storage.delete(connection, row["id"])
            connection.execute("DELETE FROM ticket_attachments WHERE id = ?", (row["id"],))
            ledger.bump_revision(connection, project)
        if storage.kind == "filesystem":
            try:
                storage.delete(None, row["id"])
            except OSError:
                app.logger.warning("A removed ticket attachment needs filesystem cleanup.")

    @app.delete("/api/v1/tasks/<reference>/attachments/<key>")
    def delete_ticket_attachment(reference, key):
        remove_attachment(reference, key, current_actor(required=True, write=True))
        return "", 204

    def invoke(name, data, actor):
        reference = data["id"]
        if name == "list-attachments":
            return list_attachments(reference, actor, AGENT_UPLOAD_BYTES)
        if name == "upload-attachment":
            result, _ = upload_attachment(reference, actor, lambda: agent_upload(data))
            return {**result, "receipt": "Ticket attachment saved."}
        key = data["attachment_id"]
        if name == "remove-attachment":
            remove_attachment(reference, key, actor)
            return {"receipt": "Ticket attachment removed."}
        offset = str(data.get("offset", "0"))
        if not re.fullmatch(r"[0-9]{1,7}", offset) or int(offset) > MAX_UPLOAD_BYTES:
            raise AttachmentError("Supply a valid byte offset.", 400, "invalid_attachment")
        start = int(offset)
        row, contents = read_attachment(reference, key, actor)
        if start > len(contents):
            raise AttachmentError("The byte offset is past the end of the attachment.", 400, "invalid_attachment")
        chunk = contents[start:start + AGENT_CHUNK_BYTES]
        end = start + len(chunk)
        return {"attachment": metadata(row, reference), "offset": start,
                "content_base64": b64encode(chunk).decode("ascii"),
                "next_offset": end if end < len(contents) else None}

    return invoke
