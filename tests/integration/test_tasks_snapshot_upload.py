"""Replay the signed private upload action with only fictional snapshots."""

import json
from stat import S_IMODE
import unittest
from unittest.mock import patch
from uuid import UUID

from tasks_codex_usage import configuration
from tasks_raft import CODEX_SNAPSHOT_ACTION
import test_tasks_service as fixtures


INITIAL = "2026-11-23T01:00:00+00:00"
FIRST = "2026-11-23T02:00:00+00:00"
SECOND = "2026-11-23T03:00:00+00:00"
MARKER = "Fictional SRE marker 1092"
MANIFEST = "/.well-known/raft-agent-manifest.json"
ACTION_PATH = f"/api/v1/raft/{CODEX_SNAPSHOT_ACTION}"
VIEW_PATH = "/api/v1/account/codex-usage"


def outcome(source="Fixture A", at=FIRST):
    return {
        "source": source, "attempted_at": at, "status": "success", "captured_at": at,
        "result": {
            "rateLimits": {"primary": {"usedPercent": 25}},
            "rateLimitsByLimitId": {MARKER: {"primary": {"usedPercent": 25}}},
        },
    }


class SnapshotUploadTest(unittest.TestCase):
    def setUp(self):
        self.service = fixtures.TasksServiceTest()
        self.service.setUp()
        self.addCleanup(self.service.doCleanups)
        self.uploader = self.service.provider.people["Avery"]["sub"]
        self.viewer_paths = self.snapshots("viewer")
        self.upload_paths = self.snapshots("disposable-uploads")
        self.upload_sources = json.dumps({label: str(path) for label, path in self.upload_paths.items()})
        self.usage = configuration({
            "TASKS_CODEX_USAGE_VIEWER_SUBJECT": self.service.provider.people["Sam"]["sub"],
            "TASKS_CODEX_USAGE_SOURCES": json.dumps({label: str(path) for label, path in self.viewer_paths.items()}),
        })

    def snapshots(self, name):
        folder = self.service.database.with_name(name)
        folder.mkdir(mode=0o700)
        paths = {label: folder / f"{label}.json" for label in ("Fixture A", "Fixture B")}
        for path in paths.values():
            path.write_text(json.dumps({
                "captured_at": INITIAL, "result": {"rateLimits": {"primary": {"usedPercent": 1}}},
            }))
            path.chmod(0o400)
        return paths

    def app(self, enabled=True):
        return self.service.app(portal_reader=True, codex_usage=self.usage,
                                codex_snapshot_uploader=self.uploader if enabled else None,
                                codex_snapshot_upload_sources=self.upload_sources)

    def upload(self, client, packet, *, status=200):
        raw = packet if isinstance(packet, str) else json.dumps(packet)
        response = self.service.call(client, ACTION_PATH, method="POST", status=status, json={"outcome": raw})
        self.assertNotIn(MARKER, response.get_data(as_text=True))
        if status == 200:
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        return response.json

    def test_defaults_off_and_requires_a_bound_board_with_two_sources(self):
        app = self.app(enabled=False)
        client, _ = self.service.login(app, "Avery")
        names = [entry["name"] for entry in self.service.call(client, MANIFEST).json["actions"]]
        self.assertNotIn(CODEX_SNAPSHOT_ACTION, names)
        self.upload(client, outcome(), status=404)
        with self.assertRaisesRegex(ValueError, "two configured sources"):
            self.service.app(codex_snapshot_uploader=self.uploader)
        with self.assertRaisesRegex(ValueError, "subject UUID"):
            self.service.app(codex_usage=self.usage, codex_snapshot_uploader="Avery")
        with self.assertRaisesRegex(ValueError, "UPLOAD_SOURCES"):
            self.service.app(codex_usage=self.usage, codex_snapshot_uploader=self.uploader)
        with self.assertRaisesRegex(ValueError, "server-bound Raft"):
            fixtures.create_app(self.service.database, public_url=fixtures.ORIGIN + fixtures.BASE,
                                codex_usage=self.usage, codex_snapshot_uploader=self.uploader,
                                marketplace_root=self.service.database.parent)

    def test_refuses_write_destinations_that_resolve_to_the_viewer_map(self):
        viewer_file = self.viewer_paths["Fixture A"]
        alias = viewer_file.parent.with_name("viewer-alias")
        alias.symlink_to(viewer_file.parent, target_is_directory=True)
        for unsafe_path in (viewer_file, alias / viewer_file.name):
            with self.subTest(path=unsafe_path), self.assertRaisesRegex(ValueError, "separate from the viewer"):
                self.service.app(codex_usage=self.usage, codex_snapshot_uploader=self.uploader,
                                 codex_snapshot_upload_sources=json.dumps({
                                     "Fixture A": str(unsafe_path), "Fixture B": str(self.upload_paths["Fixture B"]),
                                 }))

    def test_denies_other_principals_before_installing(self):
        app = self.app()
        uploader, _ = self.service.login(app, "Avery")
        other, _ = self.service.login(app, "Blake")
        self.service.provider.people["Quinn"]["sub"] = self.uploader
        human_with_same_subject, _ = self.service.login(app, "Quinn")
        with patch("taskboard_server.codex_snapshot.install_with_receipt") as installer:
            self.upload(app.test_client(), outcome(), status=401)
            self.upload(other, outcome(), status=404)
            self.upload(human_with_same_subject, outcome(), status=403)
            with patch.dict(self.service.provider.people["Avery"], server_id=str(UUID(int=999))):
                self.upload(uploader, outcome(), status=401)
            installer.assert_not_called()

    def test_receipts_use_the_stored_capture_and_sources_stay_independent(self):
        app = self.app()
        uploader, _ = self.service.login(app, "Avery")
        viewer, _ = self.service.login(app, "Sam")
        viewer_before = self.service.call(viewer, VIEW_PATH).json
        viewer_files = {path: (path.read_bytes(), path.stat().st_ino) for path in self.viewer_paths.values()}
        self.assertEqual([entry["usage"]["captured_at"] for entry in viewer_before["sources"]], [INITIAL, INITIAL])
        manifest = self.service.call(uploader, MANIFEST).json
        action = next(entry for entry in manifest["actions"] if entry["name"] == CODEX_SNAPSHOT_ACTION)
        self.assertEqual(action["endpoint"], {"method": "POST", "path": fixtures.BASE + ACTION_PATH})
        self.assertEqual(set(action["parameters"]), {"outcome"})
        self.assertEqual(self.upload(uploader, outcome()), {"status": "installed", "captured_at": FIRST})
        self.assertEqual(self.upload(uploader, outcome("Fixture B", SECOND)),
                         {"status": "installed", "captured_at": SECOND})
        healthy = self.upload_paths["Fixture B"].read_bytes()
        failure = {"source": "Fixture A", "attempted_at": SECOND, "status": "failure", "failure": "timeout"}
        self.assertEqual(self.upload(uploader, failure), {"status": "unavailable", "captured_at": FIRST})
        self.assertEqual(self.upload(uploader, outcome(at=INITIAL)), {"status": "skipped", "captured_at": FIRST})
        self.assertEqual(json.loads(self.upload_paths["Fixture A"].read_bytes())["status"], "unavailable")
        self.assertEqual(self.upload_paths["Fixture B"].read_bytes(), healthy)
        self.assertEqual([S_IMODE(path.stat().st_mode) for path in self.upload_paths.values()], [0o400, 0o400])
        self.service.call(uploader, VIEW_PATH, status=404)
        self.assertEqual(self.service.call(viewer, VIEW_PATH).json, viewer_before)
        self.assertEqual({path: (path.read_bytes(), path.stat().st_ino) for path in self.viewer_paths.values()},
                         viewer_files)

    def test_rejection_and_storage_error_preserve_both_files(self):
        app = self.app()
        uploader, _ = self.service.login(app, "Avery")
        before = {path: path.read_bytes() for path in (*self.viewer_paths.values(), *self.upload_paths.values())}
        for rejected in (
            outcome("Unknown"), {**outcome(), "path": MARKER}, MARKER * 7000,
        ):
            self.assertEqual(self.upload(uploader, rejected, status=400),
                             {"error": "The private snapshot outcome was rejected."})
        with patch("taskboard_server.codex_snapshot.os.replace", side_effect=OSError(MARKER)):
            self.assertEqual(self.upload(uploader, outcome(), status=503),
                             {"error": "The private snapshot installer is unavailable."})
        self.assertEqual({path: path.read_bytes() for path in before}, before)
