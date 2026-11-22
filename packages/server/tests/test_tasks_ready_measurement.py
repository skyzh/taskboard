"""Exercise observable episode boundaries and keep timing gaps explicit."""

import unittest

from tasks_ready_measurement import summarize


def at(clock):
    return f"2026-11-21T17:{clock}Z"


def status(clock, value, count=None, *, owner="Avery"):
    return {"kind": "status", "owner": owner, "at": at(clock), "status": value, "ready_count": count}


def wake(clock, reference, count=2):
    return {"kind": "wake-requested", "owner": "Avery", "at": at(clock), "reminder": reference, "ready_count": count}


def event(key, clock, action, task, *, actor="Avery", audit=None):
    return {"id": key, "at": at(clock), "action": action, "task_id": task, "actor": actor, "note": "private body",
            "context": {"audit": {"action": audit}} if audit else {}}


class ReadyMeasurementTest(unittest.TestCase):
    def test_uses_first_idle_and_existing_ledger_events(self):
        first_idle = {**status("00:00.240", "idle", 1), "ready_observed_at": at("00:00.500")}
        observations = [first_idle, wake("02:00", "#1234abcd"), status("02:01", "idle", 2),
                        status("02:07", "working"), status("03:00", "working"), status("05:00.200", "idle", 0)]
        history = [event(8, "02:06", "start", 9), event(2, "00:00", "start", 1),
                   event(7, "02:06", "start", 8), event(9, "02:12", "update", 8),
                   event(10, "02:20", "log", 8, audit="checklist-complete"),
                   event(11, "02:21", "log", 8, audit="checklist-complete"),
                   event(12, "02:22", "log", 12), event(13, "02:23", "log", 13, audit="checklist-start"),
                   event(14, "03:00", "done", 8), event(15, "04:00", "done", 3, actor="Robin"),
                   event(16, "05:00", "done", 9), event(17, "05:01", "done", 10),
                   {**event(18, "03:30", "update", 11), "context": {"closure": {}}}]
        complete, final = summarize(observations, history, {"#1234abcd": at("02:02.700")})
        self.assertEqual(complete["sampled_idle_at"], at("00:00.240"))
        self.assertEqual(complete["ready_count"], 1)
        self.assertEqual(complete["ready_observed_at"], at("00:00.500"))
        self.assertTrue(complete["complete"])
        self.assertEqual(complete["end_at"], at("05:00.200"))
        self.assertEqual(complete["working_sample_at"], at("02:07"))
        self.assertEqual(complete["sampled_work_seconds"], 173.2)
        self.assertEqual(complete["next_start"], {"id": 7, "task_id": 8, "at": at("02:06")})
        self.assertEqual(complete["start_wake"], "after-fired-wake")
        self.assertEqual(complete["wake_to_start_seconds"], 4)
        self.assertEqual(complete["wakes"], [{"at": at("02:00"), "ready_count": 2, "reminder": "#1234abcd",
                                               "fired_at": at("02:02.700")}])
        self.assertEqual((complete["checked_tasks"], complete["advanced_tasks"], complete["completed_tasks"]), (1, 2, 1))
        self.assertFalse(final["complete"])
        self.assertEqual(final["ready_count"], 0)
        self.assertNotIn("private body", str(complete))

    def test_distinguishes_start_before_later_wake_from_equal_seconds_and_unknown_firing(self):
        history = [event(1, "01:10", "start", 1)]
        first_idle = status("00:00", "idle", 2)
        rest = [status("02:00", "working"), status("04:00", "idle", 0)]
        for request, fired, expected in (
            (None, {}, "without-observed-wake"),
            (wake("01:20", "#1234abcd"), {"#1234abcd": at("01:21")}, "without-observed-wake"),
            (wake("01:10.900", "#1234abcd"), {}, "same-second-unordered"),
            (wake("00:55", "#1234abcd"), {"#1234abcd": at("01:10.010")}, "same-second-unordered"),
            (wake("00:55", "#1234abcd"), {"#5678abcd": at("00:56")}, "after-wake-request"),
            (wake("00:55", None), {}, "after-wake-request"),
        ):
            with self.subTest(expected=expected, request=request, fired=fired):
                observations = [first_idle, request, *rest] if request else [first_idle, *rest]
                measured = summarize(observations, history, fired)[0]
                self.assertEqual(measured["start_wake"], expected)
                self.assertIsNone(measured["wake_to_start_seconds"])

    def test_stopped_unavailable_and_observation_end_remain_incomplete(self):
        observations = [status("00:00", "working", owner="Robin"), status("00:00", "idle", None),
                        status("01:00", "unavailable"), status("01:10", "unavailable"),
                        status("02:00", "stopped", owner="Robin"), status("03:00", "idle", 0),
                        status("04:00", "working"), wake("02:00", "#orphaned")]
        history = [event(1, "00:05", "start", 1), event(2, "01:05", "done", 1),
                   event(3, "01:20", "start", 2, actor="Robin"), event(4, "04:10", "done", 3)]
        stopped, unavailable, ended = summarize(observations, history, {})
        self.assertTrue(all(not episode["complete"] for episode in (stopped, unavailable, ended)))
        self.assertEqual((stopped["owner"], stopped["end_status"]), ("Robin", "stopped"))
        self.assertIsNone(stopped["sampled_idle_at"])
        self.assertIsNone(stopped["advanced_tasks"])
        self.assertIsNone(stopped["next_start"])
        self.assertEqual(unavailable["end_status"], "unavailable")
        self.assertIsNone(unavailable["ready_count"])
        self.assertEqual(unavailable["next_start"]["id"], 1)
        self.assertEqual(unavailable["completed_tasks"], 0)
        self.assertEqual((ended["end_status"], ended["ready_count"]), ("observation-ended", 0))
        self.assertEqual(ended["wakes"], [])
        self.assertEqual(ended["completed_tasks"], 0)

    def test_repeated_idle_samples_keep_multiple_wakes_in_the_same_episode(self):
        observations = [status("00:00", "idle", 2), wake("02:00", "#first"), status("02:01", "idle", 2),
                        wake("04:00", "#second"), status("04:01", "working"), status("05:00", "idle", 0)]
        first, final = summarize(observations, [event(1, "04:10", "start", 1)],
                                 {"#first": at("02:02"), "#second": at("04:02")})
        self.assertEqual(first["sampled_idle_at"], at("00:00"))
        self.assertEqual([request["reminder"] for request in first["wakes"]], ["#first", "#second"])
        self.assertEqual(first["wake_to_start_seconds"], 8)
        self.assertEqual(final["sampled_idle_at"], at("05:00"))
        self.assertEqual(final["wakes"], [])

    def test_verified_end_bounds_partial_counts_after_last_status_sample(self):
        observations = [status("00:00", "idle", 1), status("01:00", "working")]
        history = [event(1, "02:00", "start", 1), event(2, "02:01", "done", 1),
                   event(3, "03:00", "done", 2)]
        censored = summarize(observations, history, {})[0]
        self.assertEqual(censored["end_at"], at("01:00"))
        self.assertIsNone(censored["next_start"])
        for reason in ("stopped", "timed-out", "unobserved"):
            with self.subTest(reason=reason):
                end = {"kind": "end", "owner": "Avery", "at": at("03:00"), "status": reason}
                partial, = summarize([*observations, end], history, {})
                self.assertFalse(partial["complete"])
                self.assertEqual((partial["end_at"], partial["end_status"]), (at("03:00"), reason))
                self.assertEqual(partial["working_sample_at"], at("01:00"))
                self.assertIsNone(partial["sampled_work_seconds"])
                self.assertEqual(partial["next_start"]["id"], 1)
                self.assertEqual(partial["completed_tasks"], 1)
