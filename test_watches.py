"""Verify historical local sources without reviving local source writes."""

import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

import tasks_core as ledger
import tasks_profiles as profiles
from tasks_queries import BoardQueries
from tasks_service import INTAKE_ERRORS
import watches


class WatchesTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name).resolve()
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        ledger.ensure_schema(self.db)
        profiles.ensure_principals(self.db)
        self.db.execute(profiles.IMPORT_SCHEMA)
        self.db.execute(INTAKE_ERRORS)
        self.db.execute("INSERT INTO tasks (id, title, dri, brief) VALUES (1, 'Sample release', 'Avery', '')")
        ledger.record(self.db, 1, "add", "Avery", "ready", "Review the evidence.", None, [])
        self.original = self.folder / "Original note.md"
        self.original.write_bytes(b"Historical fixture text.\n")
        self.db.execute(
            "INSERT INTO sources (id, kind, source_key, target, digest) VALUES (1, 'tasknote', ?, ?, ?)",
            ("historical-key", str(self.original), "saved-digest"),
        )
        self.db.execute("INSERT INTO task_sources (task_id, source_id) VALUES (1, 1)")
        self.shared = BoardQueries(self.db, "https://example.test")
        self.local = BoardQueries(self.db, "http://127.0.0.1", local_links=True)

    def test_legacy_links_stay_available_locally_and_keep_the_stored_identity(self):
        source_before = dict(watches.find_source(self.db, "tasknote", "historical-key"))
        for kind, url in (("github", "https://github.com/example/board/issues/1"),
                          ("jira", "https://example.test/issue/2")):
            with self.subTest(kind=kind):
                self.assertIsNotNone(watches.link_source(self.db, 1, kind, f"key-{kind}", url))
                self.assertIsNone(watches.link_source(self.db, 1, kind, f"key-{kind}", url))
        watches.link_reference(self.db, 1, "https://example.test/guide", "TaskNote guide")
        local = {link["label"]: link for link in self.local.detail(1)["sources"]}
        shared = {link["label"]: link for link in self.shared.detail(1)["sources"]}
        self.assertEqual(local["Local file"], {"label": "Local file", "url": self.original.as_uri(), "unavailable": False})
        self.assertEqual(shared["Local file"], {"label": "Local file", "url": None, "unavailable": True})
        self.assertEqual({name: item for name, item in local.items() if name != "Local file"},
                         {name: item for name, item in shared.items() if name != "Local file"})
        self.assertEqual(set(shared), {"Local file", "GitHub · key-github", "Jira · key-jira", "TaskNote guide"})
        self.assertEqual(dict(watches.find_source(self.db, "tasknote", "historical-key")), source_before)
        self.assertEqual(self.original.read_bytes(), b"Historical fixture text.\n")
        self.assertEqual([row["task_id"] for row in watches.linked_tasks(self.db, 1)], [1])

    def test_retired_kind_cannot_attach_even_when_the_local_file_exists(self):
        with self.assertRaisesRegex(ValueError, "Choose a GitHub or Jira source"):
            watches.link_source(self.db, 1, "tasknote", "new-key", self.original)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM task_sources").fetchone()[0], 1)

    def test_projected_history_preserves_user_prose_file_evidence_and_legacy_rows(self):
        watches.watch(self.db, 1, "Morgan")
        profiles.bind_legacy(self.db, "11111111-1111-4111-8111-111111111111", "agent",
                             "22222222-2222-4222-8222-222222222222", "TaskNote")
        file_uri = self.original.as_uri()
        old_uri = (self.folder / "Old.md").as_uri()
        generated_move = "Updated the path for TaskNote · historical-key."
        ordinary_links = [("TaskNote · authored evidence", file_uri)]
        scan = ledger.record(self.db, 1, "source-event", "TaskNote", "ready", "TaskNote file changed.", None,
                             [("TaskNote · historical-key", file_uri), *ordinary_links], {"source_kind": "tasknote"})
        move = ledger.record(self.db, 1, "log", "Avery", "ready", generated_move, None,
                             [("Previous location", old_uri), ("Current TaskNote", file_uri), *ordinary_links])
        authored = ledger.record(self.db, 1, "log", "TaskNote", "ready", generated_move, None, ordinary_links,
                                 {"activity_details": "TaskNote file changed."})
        attached = ledger.record(self.db, 1, "log", "Avery", "ready", "Source attached.", None, [], {
            "audit": {"action": "source-attach", "source_kind": "tasknote", "source_id": 1},
        })
        stored = list(self.db.execute("SELECT * FROM events ORDER BY id"))
        by_id = {event["id"]: event for event in self.shared.events(number=1)["items"]}
        self.assertEqual((by_id[scan]["actor"], by_id[scan]["note"], by_id[scan]["actor_profile"]),
                         ("Local file", "Local file changed.", None))
        self.assertEqual(by_id[scan]["context"], {"source_kind": "local-file"})
        self.assertEqual(by_id[scan]["links"], [
            {"label": "Local file", "url": None, "unavailable": True},
            {"label": "TaskNote · authored evidence", "url": None, "unavailable": True},
        ])
        self.assertEqual((by_id[move]["note"], [link["label"] for link in by_id[move]["links"]]),
                         ("Local file path updated.", ["Previous location", "Current local file", "TaskNote · authored evidence"]))
        self.assertEqual((by_id[authored]["actor"], by_id[authored]["note"], by_id[authored]["details"]),
                         ("TaskNote", generated_move, "TaskNote file changed."))
        self.assertEqual(by_id[authored]["actor_profile"]["name"], "TaskNote")
        self.assertEqual(by_id[attached]["context"]["audit"],
                         {"action": "source-attach", "source_kind": "local-file", "source_id": 1})
        self.assertEqual([event["id"] for event in self.shared.events(actor="Local file")["items"]], [scan])
        self.assertIn(scan, {event["id"] for event in self.shared.related_activity("Morgan", before=None, limit=10)["items"]})
        self.assertIn(scan, {event["id"] for event in self.shared.events(watcher="Morgan")["items"]})
        local_event, = self.local.events(event_id=scan)["items"]
        self.assertEqual([link["url"] for link in local_event["links"]], [file_uri, file_uri])
        self.assertEqual(list(self.db.execute("SELECT * FROM events ORDER BY id")), stored)


if __name__ == "__main__":
    unittest.main()
