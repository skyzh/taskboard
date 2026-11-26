"""Exercise Summary presence with fictional signed sessions."""

from contextlib import closing
from datetime import datetime, timezone
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

import test_tasks_service as service
from taskboard_server import reports_presence
import tasks_core as ledger
import tasks_storage as storage
from tasks_extensions import STAMP_REVIEWS
from tasks_stamp import Approval, refresh_approvals


ROUTE = "/api/v1/reports/presence"
ORIGIN = {"Origin": service.ORIGIN}
ALL = {"scope": "all"}
STAMP_PROJECT_EXTENSIONS = {"T": {STAMP_REVIEWS: "example/board"}}


class ReportsPresenceTest(unittest.TestCase):
    def test_summary_presence(self):
        fixture = service.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        admin = fixture.provider.people["Sam"]["sub"]
        self.replay(fixture, fixture.app(portal_reader=True, project_admin_subject=admin))

    def test_recent_agent_activity(self):
        fixture = service.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        admin = fixture.provider.people["Sam"]["sub"]
        app = fixture.app(portal_reader=True, project_admin_subject=admin)
        self.replay_agent_activity(fixture, app)

    def test_stamp_poll_does_not_count_the_reviewer(self):
        fixture = service.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        app = fixture.app(portal_reader=True, project_extensions=STAMP_PROJECT_EXTENSIONS)
        self.replay_stamp_poll(fixture, app)

    def replay_stamp_poll(self, fixture, app, *, postgres_dsn=None):
        owner, owner_key = fixture.login(app, "Avery")
        reviewer, reviewer_key = fixture.login(app, "Blake")
        viewer, _ = fixture.login(app, "Sam")
        pull = "https://github.com/example/board/pull/8"
        head, base, evidence = "a" * 40, "b" * 40, "c" * 32
        evidence_url = f"https://stamp.build/s/fixture/github/example/board/pull/8/approvals/{evidence}/evidence"
        with patch.object(ledger, "timestamp", return_value="2025-01-01T00:00:00Z"):
            fixture.action(owner, "create-task", {"title": "Review fixture", "step": "Prepare review."})
            fixture.action(owner, "advance-task", {"id": "T-1", "transition": "start", "note": "Prepare review."})
            fixture.action(owner, "link-reference", {"id": "T-1", "label": "Pull request", "url": pull})
            status, output, errors = fixture.shared_cli(
                owner, "peer-ask", "create", "T-1", "Review this change.", "--to", "Blake",
                "--stamp-review", pull, head, base,
            )
            self.assertEqual((status, errors), (0, ""))
            self.assertIn("T-2", output)

        def active_agents():
            return fixture.call(viewer, ROUTE, method="POST", headers=ORIGIN, json={
                "view_id": str(UUID(int=1)), "visit": 1, "project": "T",
            }).json["active_agents"]

        now = datetime(2025, 1, 1, 0, 6, tzinfo=timezone.utc)
        with (patch.object(reports_presence, "time", return_value=now.timestamp()),
              patch.object(ledger, "timestamp", return_value="2025-01-01T00:06:00Z")):
            self.assertEqual(active_agents(), 0)
            subject = fixture.provider.people["Blake"]["sub"]
            reader = Mock()
            reader.approvals.return_value = [
                Approval(evidence, evidence_url, subject, service.SERVER, head, base, ready_to_merge=True),
            ]
            connection = storage.Postgres(postgres_dsn) if postgres_dsn else storage.open_sqlite(fixture.database)
            with closing(connection) as db:
                self.assertEqual(refresh_approvals(db, reader, STAMP_PROJECT_EXTENSIONS, pull),
                                 {"recorded": 1, "ready_for_acknowledgment": 1})

            source = fixture.action(owner, "task", query_string={"id": "T-1"})["task"]
            self.assertEqual((source["state"], source["dri"], source["note"]),
                             ("waiting", owner_key, "The peer result is ready for acknowledgment."))
            request = fixture.action(owner, "task", query_string={"id": "T-2"})["task"]
            self.assertEqual((request["state"], request["dri"]), ("ready", owner_key))
            self.assertEqual(fixture.action(owner, "my-next")["work"]["key"], "T-2")
            self.assertEqual(active_agents(), 0)
            fixture.action(reviewer, "record-activity", {"id": "T-1", "note": "Verified another sample."})
            self.assertEqual(active_agents(), 1)
            events = fixture.action(owner, "task-activity", query_string={"id": "T-1"})["items"]
            self.assertEqual(events[0]["actor"], reviewer_key)

    def replay_agent_activity(self, fixture, app, *, postgres_dsn=None):
        sam, sam_id = fixture.login(app, "Sam")
        quinn, _ = fixture.login(app, "Quinn")
        avery, avery_id = fixture.login(app, "Avery")
        fixture.provider.issued.discard("Avery")
        second_session, _ = fixture.login(app, "Avery")
        blake, blake_id = fixture.login(app, "Blake")
        fixture.call(sam, "/api/v1/projects", method="POST", json={
            "prefix": "LAB", "title": "Fictional restricted work", "visibility": "ring1",
        })
        for principal in (avery_id, blake_id):
            fixture.call(sam, "/api/v1/projects/LAB/ring1", method="PUT", json={
                "principal": principal, "allowed": True,
            })

        now = 1_800_000_000

        def at(seconds):
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

        with patch.object(ledger, "timestamp", return_value=at(now - 300)):
            fixture.action(avery, "create-task", {"title": "Public fixture", "step": "Check the sample."})
            fixture.action(second_session, "record-activity", {"id": "T-1", "note": "The same writer used a second session."})
        with patch.object(ledger, "timestamp", return_value=at(now - 10)):
            for writer in (avery, blake):
                fixture.action(writer, "create-task", {"project": "LAB", "title": "Private fixture", "step": "Check it."})
        fixture.action(blake, "my-next")
        ticket = fixture.action(avery, "task", query_string={"id": "T-1"})["task"]["id"]
        connection = storage.Postgres(postgres_dsn) if postgres_dsn else storage.open_sqlite(fixture.database)
        with closing(connection) as db, storage.write(db):
            ledger.record(db, ticket, "log", blake_id, "ready", "Expired contribution.", None, [], at=at(now - 301))
            ledger.record(db, ticket, "log", sam_id, "ready", "Human contribution.", None, [], at=at(now - 5))
            ledger.record(db, ticket, "source-event", blake_id, "ready", "Imported source.", None, [], at=at(now - 5))
            for context in (
                {"recurring": "R-1"}, {"extension": "github-events"}, {"stamp_verdict": {"id": "sample"}},
                {"demo_comparison": {"pull": "sample"}}, {"watcher_measurements": {"sample": True}},
                {"audit": {"action": "worker-check"}},
            ):
                ledger.record(db, ticket, "log", blake_id, "ready", "Automated fixture.", None, [], context, at=at(now - 5))

        def presence(client, tab, scope, status=200):
            return fixture.call(client, ROUTE, method="POST", headers=ORIGIN, json={
                "view_id": str(UUID(int=tab)), "visit": 1, **scope,
            }, status=status).json

        with patch.object(reports_presence, "time", return_value=now) as clock:
            self.assertEqual(presence(quinn, 1, ALL), {"count": 1, "active_agents": 1})
            self.assertEqual(presence(sam, 1, ALL), {"count": 2, "active_agents": 2})
            self.assertEqual(presence(sam, 2, {"project": "T"}), {"count": 1, "active_agents": 1})
            self.assertEqual(presence(sam, 3, {"project": "LAB"}), {"count": 1, "active_agents": 2})
            presence(quinn, 2, {"project": "LAB"}, status=404)
            clock.return_value = now + 1
            self.assertEqual(presence(quinn, 1, ALL), {"count": 2, "active_agents": 0})
            self.assertEqual(presence(sam, 1, ALL), {"count": 2, "active_agents": 2})
            clock.return_value = now + 291
            self.assertEqual(presence(sam, 1, ALL), {"count": 1, "active_agents": 0})

    def replay(self, fixture, app):
        call = fixture.call
        sam, _ = fixture.login(app, "Sam")
        fixture.provider.issued.discard("Sam")
        second_device, _ = fixture.login(app, "Sam")
        quinn, quinn_id = fixture.login(app, "Quinn")
        avery, avery_id = fixture.login(app, "Avery")
        anonymous = app.test_client()

        def post(client, tab, scope, *, visit=1, leave=False, status=200, headers=ORIGIN):
            data = {"view_id": str(UUID(int=tab)), "visit": visit, **scope}
            if leave:
                data["leave"] = True
            response = call(client, ROUTE, method="POST", json=data, headers=headers, status=status)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            return response.json

        def count(client, tab, scope, expected, **options):
            self.assertEqual(post(client, tab, scope, **options), {"count": expected, "active_agents": 0})

        t = {"project": "T"}
        lab = {"project": "LAB"}
        call(sam, "/api/v1/projects", method="POST", json={
            "prefix": "LAB", "title": "Fictional restricted project", "visibility": "ring1",
        })
        membership = "/api/v1/projects/LAB/ring1"

        def grant(principal, allowed=True):
            call(sam, membership, method="PUT", json={"principal": principal, "allowed": allowed})

        post(anonymous, 1, t, status=401)
        post(sam, 1, t, status=403, headers={})
        post(sam, 1, t, status=403, headers={"Origin": "https://elsewhere.example"})
        post(quinn, 1, lab, status=404)
        grant(avery_id)
        post(avery, 1, lab, status=403)
        grant(quinn_id)
        for data in ({}, {"scope": None}, {"scope": "all", "project": "T"}, {"project": None}):
            post(sam, 1, data, status=400)
        original = call(sam, "/api/v1/revision").json
        activity = call(sam, "/api/v1/activity?scope=all&detail=all").json

        with patch.object(reports_presence, "time", return_value=1000) as clock:
            count(sam, 1, t, 1)
            count(sam, 2, t, 1)
            count(second_device, 3, t, 1)
            count(quinn, 1, lab, 1)
            count(sam, 1, ALL, 1, visit=2)
            count(sam, 1, t, 1, leave=True)
            count(sam, 1, ALL, 1, visit=2)
            count(sam, 2, t, 1, leave=True)
            count(second_device, 3, t, 0, leave=True)
            count(quinn, 2, t, 1)
            count(sam, 1, ALL, 1, visit=2)
            count(sam, 1, lab, 2, visit=3)
            count(sam, 1, ALL, 0, visit=2, leave=True)
            count(sam, 1, lab, 2, visit=3)
            self.assertEqual(call(sam, "/api/v1/revision").json, original)
            self.assertEqual(call(sam, "/api/v1/activity?scope=all&detail=all").json, activity)

            grant(quinn_id, False)
            post(quinn, 1, lab, status=404)
            count(sam, 1, lab, 1, visit=3)
            count(sam, 2, t, 2, visit=2)
            clock.return_value += 20
            count(sam, 2, t, 2, visit=2)
            clock.return_value += 25
            count(sam, 2, t, 1, visit=2)
            count(sam, 1, lab, 0, visit=3, leave=True)

            count(sam, 1, ALL, 1, visit=4)
            count(sam, 1, ALL, 1, visit=5)
            count(sam, 1, ALL, 1, visit=4, leave=True)
            count(sam, 1, t, 1, visit=4)
            count(quinn, 2, ALL, 2, visit=2)
            count(sam, 1, ALL, 1, visit=5, leave=True)
            count(sam, 1, ALL, 1, visit=5)
            count(sam, 4, ALL, 1, leave=True)
            count(sam, 4, ALL, 1)
            count(quinn, 2, ALL, 1, visit=2)
            count(sam, 1, ALL, 2, visit=6)
