"""Exercise Summary presence with fictional signed sessions."""

import unittest
from unittest.mock import patch
from uuid import UUID

import test_tasks_service as service
from taskboard_server import reports_presence


ROUTE = "/api/v1/reports/presence"
ORIGIN = {"Origin": service.ORIGIN}
ALL = {"scope": "all"}


class ReportsPresenceTest(unittest.TestCase):
    def test_summary_presence(self):
        fixture = service.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        admin = fixture.provider.people["Sam"]["sub"]
        self.replay(fixture, fixture.app(portal_reader=True, project_admin_subject=admin))

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
            self.assertEqual(post(client, tab, scope, **options), {"count": expected})

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
