"""Codex usage stays private to the configured human and shows the supplied fields."""

import json
import unittest
from unittest.mock import patch

from tasks_codex_usage import CodexUsage, configuration
import test_tasks_service as fixtures


class CodexUsageTest(unittest.TestCase):
    def test_only_the_configured_human_can_read_the_sample(self):
        fixture = fixtures.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        path = fixture.database.with_name("sample.json")
        path.write_text(json.dumps({
            "sample": True, "captured_at": "2026-11-01T12:00:00Z",
            "result": {
                "private_account_detail": "synthetic-do-not-copy",
                "rateLimits": {"credits": {"balance": "25.50", "unlimited": False}},
                "rateLimitsByLimitId": {"codex": {
                    "primary": {"usedPercent": 26.5, "windowDurationMins": 300, "resetsAt": 1793548800},
                    "secondary": None,
                }},
                "rateLimitResetCredits": {"availableCount": 0, "credits": ["synthetic-do-not-copy"]},
            },
        }))
        usage = configuration({
            "TASKS_CODEX_USAGE_VIEWER_SUBJECT": fixture.provider.people["Sam"]["sub"],
            "TASKS_CODEX_USAGE_SNAPSHOT": str(path),
        })
        app = fixture.app(portal_reader=True, codex_usage=usage)
        owner, _ = fixture.login(app, "Sam")
        other, _ = fixture.login(app, "Quinn")
        agent, _ = fixture.login(app, "Avery")
        endpoint = "/api/v1/account/codex-usage"
        with patch.object(CodexUsage, "read", autospec=True) as read:
            fixture.call(app.test_client(), endpoint, status=401)
            fixture.call(other, endpoint, status=404)
            fixture.call(agent, endpoint, status=404)
            read.assert_not_called()
        response = fixture.call(owner, endpoint)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("synthetic-do-not-copy", response.get_data(as_text=True))
        summary = response.json["usage"]
        self.assertEqual(summary, {
            "sample": True, "captured_at": "2026-11-01T12:00:00+00:00",
            "buckets": [{"label": "codex", "primary": {
                "remaining_percent": 73.5, "duration_minutes": 300, "resets_at": 1793548800,
            }, "secondary": None}],
            "paid_balance": "25.50", "paid_unlimited": False, "reset_credits": 0,
        })
        path.write_text(json.dumps({"captured_at": "2026-11-01T12:00:00Z", "result": {
            "rateLimits": {"primary": {}, "credits": {"unlimited": True}},
        }}))
        missing_fields = fixture.call(owner, endpoint).json["usage"]
        self.assertEqual(missing_fields["buckets"], [{"label": "Codex", "primary": None, "secondary": None}])
        self.assertEqual((missing_fields["paid_balance"], missing_fields["paid_unlimited"], missing_fields["reset_credits"]),
                         (None, True, None))
        path.write_text("invalid synthetic snapshot")
        self.assertIsNone(fixture.call(owner, endpoint).json["usage"])

    def test_two_sources_have_separate_timestamps_values_and_availability(self):
        fixture = fixtures.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        first = fixture.database.with_name("first.json")
        second = fixture.database.with_name("second.json")
        environment = {
            "TASKS_CODEX_USAGE_VIEWER_SUBJECT": fixture.provider.people["Sam"]["sub"],
            "TASKS_CODEX_USAGE_SOURCES": json.dumps({"Source A": str(first), "Source B": str(second)}),
        }
        app = fixture.app(portal_reader=True, codex_usage=configuration(environment))
        owner, _ = fixture.login(app, "Sam")
        other, _ = fixture.login(app, "Quinn")
        agent, _ = fixture.login(app, "Avery")
        endpoint = "/api/v1/account/codex-usage"
        with patch.object(CodexUsage, "_read_snapshot", autospec=True) as read:
            fixture.call(app.test_client(), endpoint, status=401)
            fixture.call(other, endpoint, status=404)
            fixture.call(agent, endpoint, status=404)
            read.assert_not_called()

        for path, at, balance in ((first, "12:00:00", "25.50"), (second, "13:00:00", "2.00")):
            path.write_text(json.dumps({"sample": True, "captured_at": f"2026-11-01T{at}Z", "result": {
                "rateLimits": {"credits": {"balance": balance}}, "credential": "synthetic-private",
            }}))
        response = fixture.call(owner, endpoint)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("synthetic-private", response.get_data(as_text=True))
        self.assertNotIn(str(first), response.get_data(as_text=True))
        a, b = response.json["sources"]
        self.assertEqual((a["label"], a["usage"]["paid_balance"], a["usage"]["captured_at"]),
                         ("Source A", "25.50", "2026-11-01T12:00:00+00:00"))
        self.assertEqual((b["label"], b["usage"]["paid_balance"], b["usage"]["captured_at"]),
                         ("Source B", "2.00", "2026-11-01T13:00:00+00:00"))
        second.write_text("invalid synthetic data")
        self.assertEqual(fixture.call(owner, endpoint).json["sources"], [a, {"label": "Source B", "usage": None}])

    def test_usage_is_disabled_by_default(self):
        fixture = fixtures.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        owner, _ = fixture.login(fixture.app(portal_reader=True), "Sam")
        fixture.call(owner, "/api/v1/account/codex-usage", status=404)
