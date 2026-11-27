"""Exercise private ticket attachments through a signed disposable board."""

from base64 import b64decode, b64encode
from contextlib import closing, redirect_stdout
import json
from subprocess import CompletedProcess
from io import BytesIO, StringIO
import os
from shutil import copytree
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_tasks_service as fixtures
from taskboard_cli import main as tasks_main
from taskboard_server.attachment_routes import AGENT_UPLOAD_BYTES
from tasks_attachments import FilesystemStorage, MAX_UPLOAD_BYTES, PostgresStorage


class AttachmentTest(unittest.TestCase):
    def setUp(self):
        self.board = fixtures.TasksServiceTest()
        self.board.setUp()
        self.addCleanup(self.board.doCleanups)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.storage = FilesystemStorage(self.root)
        self.options = {"project_admin_subject": self.board.provider.people["Sam"]["sub"]}

    def configure(self):
        self.app = self.board.app(attachment_storage=self.storage, **self.options)
        self.avery, self.avery_id = self.board.login(self.app, "Avery")
        self.blake, self.blake_id = self.board.login(self.app, "Blake")
        self.sam, _ = self.board.login(self.app, "Sam")

    def create(self, project="T"):
        result = self.board.action(self.avery, "create-task", {
            "project": project, "title": "Attachment fixture", "step": "Read the sample.",
        })
        return f"/api/v1/tasks/{result['receipt'].split()[0]}/attachments"

    def upload(self, path, upload_id, *, contents=b"sample\0data", status=201, actor=None):
        response = self.board.call(actor or self.avery, path, method="POST", status=status, data={
            "file": (BytesIO(contents), "sample ü.txt", "text/plain"), "upload_id": upload_id,
        })
        self.addCleanup(response.request.input_stream.close)
        return response.get_json()

    def test_existing_ledger_retry_download_and_delete(self):
        self.app = self.board.app(**self.options)
        self.avery, _ = self.board.login(self.app, "Avery")
        path = self.create()
        unavailable = self.board.call(self.avery, path, status=503).json
        self.assertEqual(unavailable["code"], "attachment_storage_unavailable")
        self.board.provider = fixtures.FixtureRaft()
        self.configure()
        self.assertEqual(self.board.call(self.avery, path).json, {
            "attachments": [], "max_upload_bytes": MAX_UPLOAD_BYTES, "can_edit": True,
        })
        upload_id = uuid4().hex
        receipt = self.upload(path, upload_id)
        self.assertEqual(self.upload(path, upload_id, status=200), receipt)
        self.assertEqual(self.upload(path, upload_id, contents=b"different", status=409)["code"], "upload_id_conflict")
        self.assertEqual(self.upload(path, uuid4().hex, contents=bytes(MAX_UPLOAD_BYTES + 1), status=413)["code"],
                         "attachment_too_large")
        item = receipt["attachment"]
        self.assertEqual(self.board.call(self.avery, path).json["attachments"], [item])
        download = item["download_url"].removeprefix(fixtures.BASE)
        answer = self.board.call(self.avery, download)
        self.assertEqual(answer.data, b"sample\0data")
        self.assertTrue(answer.headers["Content-Disposition"].startswith("attachment;"))
        self.assertEqual(answer.headers["Cache-Control"], "private, no-store")
        self.board.call(self.app.test_client(), download, status=401)
        self.board.call(self.avery, path + "/" + item["id"], method="DELETE", status=204)
        self.board.call(self.avery, download, status=404)
        self.assertEqual(self.board.call(self.avery, path).json["attachments"], [])

    def test_read_only_agent_and_filesystem_backup_restore(self):
        self.options["project_roster"] = {self.board.provider.people["Avery"]["sub"]: "Avery"}
        self.configure()
        path = self.create()
        item = self.upload(path, uuid4().hex)["attachment"]
        download = item["download_url"].removeprefix(fixtures.BASE)
        info = self.board.call(self.blake, path).json
        self.assertFalse(info["can_edit"])
        self.assertEqual(info["attachments"], [item])
        self.assertEqual(self.board.call(self.blake, download).data, b"sample\0data")
        self.upload(path, uuid4().hex, actor=self.blake, status=403)
        self.board.call(self.blake, path + "/" + item["id"], method="DELETE", status=403)

        restored = self.board.database.with_name("restored.sqlite3")
        with closing(sqlite3.connect(self.board.database)) as source, closing(sqlite3.connect(restored)) as target:
            source.backup(target)
        copied_root = restored.parent / "restored-files"
        copytree(self.root, copied_root)
        self.board.database = restored
        self.board.provider = fixtures.FixtureRaft()
        self.storage = FilesystemStorage(copied_root)
        self.configure()
        self.assertEqual(self.board.call(self.avery, path).json["attachments"], [item])
        self.assertEqual(self.board.call(self.avery, download).data, b"sample\0data")

    def test_private_access_is_rechecked_for_metadata_and_download(self):
        self.configure()
        self.board.call(self.sam, "/api/v1/projects", method="POST", json={
            "prefix": "LAB", "title": "Private fixtures", "visibility": "ring1",
        })
        member = "/api/v1/projects/LAB/ring1"
        for principal in (self.avery_id, self.blake_id):
            self.board.call(self.sam, member, method="PUT", json={"principal": principal, "allowed": True})
        path = self.create("LAB")
        item = self.upload(path, uuid4().hex)["attachment"]
        download = item["download_url"].removeprefix(fixtures.BASE)
        self.assertEqual(self.board.call(self.blake, download).data, b"sample\0data")
        self.board.call(self.sam, member, method="PUT", json={"principal": self.blake_id, "allowed": False})
        self.board.call(self.blake, path, status=404)
        with patch.object(self.storage, "read", side_effect=AssertionError("Access must be checked first")):
            self.board.call(self.blake, download, status=404)
        self.board.call(self.blake, path + "/" + item["id"], method="DELETE", status=404)

    def test_signed_agent_cli_uses_the_same_ticket_storage(self):
        self.configure()
        route = self.create()
        ticket = route.split("/")[-2]
        payload = bytes(range(256)) * (AGENT_UPLOAD_BYTES // 256)
        source = self.root.parent / (self.root.name + "-fixture.txt")
        self.addCleanup(source.unlink, missing_ok=True)
        source.write_bytes(payload)
        target = source.with_suffix(".download")
        self.addCleanup(target.unlink, missing_ok=True)
        upload_id = uuid4().hex

        def invoke(command, **options):
            name = command[command.index("--action") + 1]
            data = json.loads(options["input"]) if options["input"] is not None else None
            filters = dict(command[index + 1].split("=", 1) for index, value in enumerate(command) if value == "--param")
            result = self.board.action(self.avery, name, data, query_string=filters)
            return CompletedProcess(command, 0, json.dumps({"ok": True, "data": {"result": result}}), "")

        def cli(*args):
            with patch("taskboard_client.connection.subprocess.run", side_effect=invoke), redirect_stdout(StringIO()) as printed:
                self.assertEqual(tasks_main(["--shared", *args]), 0)
                return printed.getvalue()

        item = json.loads(cli("attachment-upload", ticket, str(source), "--upload-id", upload_id))
        self.assertEqual(json.loads(cli("attachment-upload", ticket, str(source), "--upload-id", upload_id)), item)
        self.assertEqual(json.loads(cli("attachment-list", ticket))["attachments"], [item])
        cli("attachment-download", ticket, item["id"], "--output", str(target))
        self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(self.board.call(self.avery, item["download_url"].removeprefix(fixtures.BASE)).data, payload)
        cli("attachment-remove", ticket, item["id"])
        self.assertEqual(self.board.call(self.avery, route).json["attachments"], [])

    def test_agent_reads_browser_bytes_in_bounded_chunks(self):
        self.configure()
        path = self.create()
        ticket = path.split("/")[-2]
        payload = bytes(range(256)) * 768
        item = self.upload(path, uuid4().hex, contents=payload)["attachment"]
        params = {"id": ticket, "attachment_id": item["id"]}
        offset = 0
        chunks = []
        while offset is not None:
            result = self.board.action(self.blake, "read-attachment-chunk", query_string={**params, "offset": offset})
            self.assertEqual(result["offset"], offset)
            self.assertEqual(result["attachment"], item)
            decoded = b64decode(result["content_base64"], validate=True)
            self.assertLessEqual(len(decoded), 32 * 1024)
            chunks.append(decoded)
            offset = result["next_offset"]
        self.assertEqual(b"".join(chunks), payload)
        too_large = b64encode(bytes(AGENT_UPLOAD_BYTES + 1)).decode("ascii")
        self.board.action(self.avery, "upload-attachment", {
            "id": ticket, "upload_id": uuid4().hex, "filename": "fixture.txt",
            "content_type": "text/plain", "content_base64": too_large,
        }, status=413)

    def test_external_adapter_removes_the_object_after_commit(self):
        class FixtureStorage:
            kind = "fixture"
            delete_in_transaction = False

            def __init__(self):
                self.objects = {}

            def write(self, db, key, contents):
                if key in self.objects:
                    raise FileExistsError(key)
                self.objects[key] = contents

            def read(self, db, key):
                try:
                    return self.objects[key]
                except KeyError as error:
                    raise FileNotFoundError(key) from error

            def delete(self, db, key):
                self.objects.pop(key, None)

        self.storage = FixtureStorage()
        self.configure()
        path = self.create()
        item = self.upload(path, uuid4().hex)["attachment"]
        key = item["id"]
        with patch.object(self.storage, "delete", wraps=self.storage.delete) as delete:
            self.board.call(self.avery, path + "/" + key, method="DELETE", status=204)
        delete.assert_called_once_with(None, key)
        self.assertEqual(self.storage.objects, {})
        self.assertEqual(self.board.call(self.avery, path).json["attachments"], [])

    def test_failed_filesystem_publish_is_retryable_without_metadata(self):
        self.configure()
        path = self.create()
        upload_id = uuid4().hex
        with patch("tasks_attachments.os.link", side_effect=OSError("fictional failure")):
            self.assertEqual(self.upload(path, upload_id, status=503)["code"], "attachment_storage_unavailable")
        self.assertEqual(self.board.call(self.avery, path).json["attachments"], [])
        self.assertEqual(list(self.root.iterdir()), [])
        self.upload(path, upload_id)


@unittest.skipUnless(os.environ.get("TASKBOARD_ATTACHMENTS_TEST_DSN"), "requires a disposable PostgreSQL database")
class PostgresAttachmentTest(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.conninfo import make_conninfo

        database = os.environ["TASKBOARD_ATTACHMENTS_TEST_DSN"]
        schema = "attachments_" + uuid4().hex
        self.connection = psycopg.connect(database, autocommit=True)
        self.addCleanup(self.connection.close)
        self.connection.execute(f"CREATE SCHEMA {schema}")
        self.addCleanup(self.connection.execute, f"DROP SCHEMA {schema} CASCADE")
        self.dsn = make_conninfo(database, options=f"-c search_path={schema}")

    def test_sqlite_snapshot_import_preserves_filesystem_attachment(self):
        board = fixtures.TasksServiceTest()
        board.setUp()
        self.addCleanup(board.doCleanups)
        files = board.database.parent / "attachment-files"
        files.mkdir()
        app = board.app(attachment_storage=FilesystemStorage(files))
        avery, _ = board.login(app, "Avery")
        created = board.action(avery, "create-task", {"title": "Migration fixture", "step": "Read the sample."})
        ticket = created["receipt"].split()[0]
        path = f"/api/v1/tasks/{ticket}/attachments"
        contents = b"original attachment bytes"
        item = board.call(avery, path, method="POST", status=201, data={
            "file": (BytesIO(contents), "sample.txt"), "upload_id": uuid4().hex,
        }).json["attachment"]
        snapshot = board.database.with_name("attachment-backup.sqlite3")
        with closing(sqlite3.connect(board.database)) as source, closing(sqlite3.connect(snapshot)) as target:
            source.backup(target)
        restored_files = files.with_name("restored-attachment-files")
        copytree(files, restored_files)

        with patch.dict(os.environ, {"TASKS_POSTGRES_DSN": self.dsn}), redirect_stdout(StringIO()) as printed:
            self.assertEqual(tasks_main(["--postgres", "postgres-import", str(snapshot)]), 0)
            counts = json.loads(printed.getvalue())
        self.assertEqual((counts["ticket_attachments"], counts["ticket_attachment_bytes"]), (1, 0))
        board.database = None
        board.provider = fixtures.FixtureRaft()
        app = board.app(postgres_dsn=self.dsn, attachment_storage=FilesystemStorage(restored_files))
        signed, _ = board.login(app, "Avery")
        self.assertEqual(board.call(signed, path).json["attachments"], [item])
        download = item["download_url"].removeprefix(fixtures.BASE)
        self.assertEqual(board.call(signed, download).data, contents)
        result = board.action(signed, "read-attachment-chunk", query_string={
            "id": ticket, "attachment_id": item["id"],
        })
        self.assertEqual(result["attachment"], item)
        self.assertEqual(b64decode(result["content_base64"]), contents)
        self.assertIsNone(result["next_offset"])

    def test_shared_route_commits_and_deletes_bytea(self):
        board = fixtures.TasksServiceTest()
        board.setUp()
        self.addCleanup(board.doCleanups)
        board.database = None
        app = board.app(postgres_dsn=self.dsn, attachment_storage=PostgresStorage())
        avery, _ = board.login(app, "Avery")
        created = board.action(avery, "create-task", {"title": "Bytea fixture", "step": "Read the sample."})
        path = f"/api/v1/tasks/{created['receipt'].split()[0]}/attachments"
        item = board.call(avery, path, method="POST", status=201, data={
            "file": (BytesIO(b"postgres\0bytes"), "sample.txt"), "upload_id": uuid4().hex,
        }).json["attachment"]
        download = item["download_url"].removeprefix(fixtures.BASE)
        self.assertEqual(board.call(avery, download).data, b"postgres\0bytes")
        board.call(avery, path + "/" + item["id"], method="DELETE", status=204)
        board.call(avery, download, status=404)
