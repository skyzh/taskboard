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

    def test_usage_is_disabled_by_default(self):
        fixture = fixtures.TasksServiceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        owner, _ = fixture.login(fixture.app(portal_reader=True), "Sam")
        fixture.call(owner, "/api/v1/account/codex-usage", status=404)
