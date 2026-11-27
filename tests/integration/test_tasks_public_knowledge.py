"""Replay signed publication and anonymous reads on a disposable shared board."""

from contextlib import closing
from io import BytesIO
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import test_tasks_service as fixtures
from tasks_attachments import FilesystemStorage


PUBLIC = "/api/v1/public/knowledge"


class PublicKnowledgeTest(unittest.TestCase):
    def setUp(self):
        self.board = fixtures.TasksServiceTest()
        self.board.setUp()
        self.addCleanup(self.board.doCleanups)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        files = Path(directory.name)
        self.storage = FilesystemStorage(files)
        dist = files.parent / (files.name + "-dist")
        dist.mkdir()
        self.addCleanup(dist.rmdir)
        document = dist / "index.html"
        document.write_text('<html><head><base href="./"></head></html>', encoding="utf-8")
        self.addCleanup(document.unlink)
        self.app = self.board.app(
            attachment_storage=self.storage, dist=dist,
            project_admin_subject=self.board.provider.people["Sam"]["sub"],
            project_roster={self.board.provider.people["Avery"]["sub"]: "Avery"},
        )
        self.avery, self.avery_id = self.board.login(self.app, "Avery")
        self.blake, _ = self.board.login(self.app, "Blake")
        self.sam, _ = self.board.login(self.app, "Sam")
        self.anonymous = self.app.test_client()

    def create(self, project="T"):
        row = self.board.action(self.avery, "create-knowledge", {
            "project": project, "title": "Private working title", "brief": "Internal draft details.",
        })
        return f"/api/v1/knowledge/{row['id']}/publication"

    def publish(self, path, *, title="Public example", body="Reviewed body.", attachment_ids=(), actor=None, status=200):
        return self.board.call(actor or self.avery, path, method="PUT", status=status, json={
            "title": title, "body": body, "attachment_ids": list(attachment_ids),
        }).json

    def upload(self):
        result = self.board.action(self.avery, "create-task", {
            "project": "T", "title": "Example ticket", "step": "Inspect the fixture.",
        })
        ticket = result["receipt"].split()[0]
        route = f"/api/v1/tasks/{ticket}/attachments"
        response = self.board.call(self.avery, route, method="POST", status=201, data={
            "file": (BytesIO(b"sample\0bytes"), "example.txt", "text/plain"), "upload_id": uuid4().hex,
        })
        self.addCleanup(response.request.input_stream.close)
        return route, response.json["attachment"]["id"]

    def test_unpublished_drafts_and_private_fields_never_appear(self):
        route = self.create()
        self.create()
        self.board.call(self.anonymous, route, status=401)
        self.board.call(self.anonymous, "/api/v1/knowledge", status=401)
        self.assertEqual(self.board.call(self.anonymous, PUBLIC).json["items"], [])
        self.publish(route, actor=self.blake, status=403)
        published = self.publish(route)["publication"]
        entry = f"{PUBLIC}/{published['id']}"
        response = self.board.call(self.anonymous, entry)
        self.assertEqual(self.publish(route)["publication"], published)
        public_document = urlsplit(published["url"]).path.removeprefix(fixtures.BASE)
        self.assertIn(f'<base href="{fixtures.BASE}/">'.encode(), self.board.call(self.anonymous, public_document).data)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(set(response.json), {"id", "title", "excerpt", "published_at", "url", "body", "attachments"})
        self.assertEqual(response.json["body"], "Reviewed body.")
        self.assertEqual(self.board.call(self.anonymous, PUBLIC, query_string={"q": "Internal draft"}).json["items"], [])
        self.assertEqual(len(self.board.call(self.anonymous, PUBLIC, query_string={"q": "Reviewed"}).json["items"]), 1)
        ticket = route.split("/")[-2]
        self.board.call(self.avery, f"/api/v1/tasks/{ticket}/details", method="POST", json={
            "reason": "Revise the private draft", "brief": "Changed private draft.",
        })
        self.assertEqual(self.board.call(self.anonymous, entry).json["body"], "Reviewed body.")
        self.board.call(self.avery, route, method="DELETE")
        self.assertEqual(self.board.call(self.anonymous, entry, status=404).headers["Cache-Control"], "no-store")
        self.assertEqual(self.publish(route, body="New reviewed body.")["publication"]["id"], published["id"])
        with closing(sqlite3.connect(self.board.database)) as db:
            self.assertEqual(
                [tuple(row) for row in db.execute("""
                    SELECT operation, actor FROM audit_events
                    WHERE record_type = 'knowledge_publication' ORDER BY id
                """)],
                [(operation, self.avery_id) for operation in ("publish", "unpublish", "publish")],
            )

    def test_source_files_are_opt_in_and_prior_grants_cannot_be_reused(self):
        route = self.create()
        source, attachment = self.upload()
        unrelated_source, unrelated_attachment = self.upload()
        published = self.publish(route, attachment_ids=(attachment,))["publication"]
        entry = f"{PUBLIC}/{published['id']}"
        files = self.board.call(self.anonymous, entry).json["attachments"]
        self.assertEqual(len(files), 1)
        self.assertNotEqual(files[0]["id"], attachment)
        download = urlsplit(files[0]["download_url"]).path.removeprefix(fixtures.BASE)
        response = self.board.call(self.anonymous, download, headers={"Range": "bytes=0-5"}, status=206)
        self.assertEqual(response.data, b"sample")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertTrue(response.headers["Content-Disposition"].startswith("attachment;"))
        self.board.call(self.anonymous, f"{entry}/attachments/{unrelated_attachment}", status=404)
        self.board.call(self.anonymous, f"{unrelated_source}/{unrelated_attachment}/download", status=401)
        self.publish(route)
        with patch.object(self.storage, "read", side_effect=AssertionError("Revoked file must not be opened")):
            self.board.call(self.anonymous, download, status=404)
        self.publish(route, attachment_ids=(attachment,))
        files = self.board.call(self.anonymous, entry).json["attachments"]
        replacement = urlsplit(files[0]["download_url"]).path.removeprefix(fixtures.BASE)
        self.assertNotEqual(replacement, download)
        self.board.call(self.avery, f"{source}/{attachment}", method="DELETE", status=204)
        self.assertEqual(self.board.call(self.anonymous, entry).json["attachments"], [])
        self.board.call(self.anonymous, replacement, status=404)

    def test_equal_time_pagination_and_ring1_filter(self):
        self.board.call(self.sam, "/api/v1/projects", method="POST", json={
            "prefix": "LAB", "title": "Private fixtures", "visibility": "ring1",
        })
        self.board.call(self.sam, "/api/v1/projects/LAB/ring1", method="PUT",
                        json={"principal": self.avery_id, "allowed": True})
        self.publish(self.create("LAB"), status=404)
        with patch("tasks_core.timestamp", return_value="2026-01-01T00:00:00+00:00"):
            first, second = [self.publish(self.create())["publication"]["id"] for _ in range(2)]
        page = self.board.call(self.anonymous, PUBLIC, query_string={"limit": 1}).json
        remaining = self.board.call(
            self.anonymous, PUBLIC, query_string={"limit": 1, "before": page["next_cursor"]},
        ).json
        self.assertEqual([page["items"][0]["id"], remaining["items"][0]["id"]], sorted((first, second), reverse=True))
        self.assertIsNone(remaining["next_cursor"])
