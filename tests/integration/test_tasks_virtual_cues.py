"""Replay server-ranked ticket cues with signed disposable identities."""

from contextlib import closing
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

from tasks_service import connect
import test_tasks_service as fixtures


class VirtualTicketCueTest(unittest.TestCase):
    def test_signed_preview_ranks_actionable_work_and_uses_no_delivery_rows(self):
        board = fixtures.TasksServiceTest()
        board.setUp()
        self.addCleanup(board.doCleanups)
        app = board.app()
        avery, avery_id = board.login(app, "Avery")
        blake, blake_id = board.login(app, "Blake")

        def create(client, title):
            row = board.action(client, "create-task", {"title": title, "step": "Review the fixture."})
            return row["receipt"].split()[0]

        def advance(client, ticket, transition, **fields):
            return board.action(client, "advance-task", {
                "id": ticket, "transition": transition, "note": "Review the fixture.", **fields,
            })

        at = "2026-11-27T21:06:00.000Z"
        with patch("tasks_core.timestamp", return_value=at):
            ready = create(avery, "Ordinary ready")
            recovery = create(avery, "Runnable recovery")
            advance(avery, recovery, "start")
            worker_ticket = create(avery, "Worker is active")
            advance(avery, worker_ticket, "start")
            run = board.action(avery, "delegate", {
                "id": worker_ticket, "worker": blake_id, "role": "build", "brief": "Check the fixture.",
            })["receipt"].split()[0]
            board.action(avery, "worker-handoff", {
                "run": run, "transition": "start", "handle": "synthetic-worker",
            })

            blocker = create(blake, "External dependency")
            advance(blake, blocker, "start")
            waiting = create(avery, "Bound waiting")
            advance(avery, waiting, "start")
            advance(avery, waiting, "wait", blocked_by=blocker)
            open_blocker = create(blake, "Unresolved blocker")
            unresolved = create(avery, "Unresolved waiting")
            advance(avery, unresolved, "start")
            advance(avery, unresolved, "wait", blocked_by=open_blocker)
            legacy_waits = []
            for number in range(5):
                legacy = create(avery, f"Historical dependency {number + 1}")
                advance(avery, legacy, "start")
                board.cli("wait", legacy, "Still blocked.", "--on", open_blocker, "--by", avery_id)
                legacy_waits.append(legacy)
            manual = create(avery, "Unbound review")
            advance(avery, manual, "start")
            board.cli("wait", manual, "Review the sample.", "--on", "Blake", "--by", avery_id)
            automation = create(avery, "Automation dependency")
            advance(avery, automation, "start")
            advance(avery, automation, "wait", on="Build", automation=True)

            source = create(avery, "Peer work")
            advance(avery, source, "start")
            request = board.action(avery, "peer-ask", {
                "id": source, "transition": "create", "recipient": blake_id, "note": "Check the sample.",
                "force_delivery": True,
            })["receipt"].split()[0]
            advance(blake, request, "start")
            board.action(blake, "peer-ask", {
                "id": request, "transition": "result", "note": "The sample passed.",
            })

            def cues(client=avery, **filters):
                rows = board.action(client, "my-ticket-cues", query_string=filters)["items"]
                return [(row["kind"], row["ticket"]) for row in rows]

            self.assertEqual(cues(), [
                ("recovery", recovery), ("peer-result", request), ("ready", ready), ("wait-review", manual),
            ])
            advance(blake, blocker, "done")
            expected = [
                ("recovery", recovery), ("peer-result", request), ("released-wait", waiting),
                ("ready", ready), ("wait-review", manual),
            ]
            self.assertEqual(cues(), expected)
            self.assertEqual(cues(limit=2), expected[:2])
            self.assertEqual(cues(blake), [("ready", open_blocker)])
            inbox = board.action(avery, "my-next")
            self.assertEqual(inbox["work"]["key"], ready)
            self.assertEqual([row["key"] for row in inbox["wait_review"]], legacy_waits)
            board.action(avery, "set-cue-pause", {
                "id": recovery, "kind": "stalled", "minutes": "1", "reason": "Checking fixture locally.",
            })
            self.assertEqual(cues(), expected[1:])
            with closing(connect(board.database)) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM wake_log").fetchone()[0], 0)

        later = (datetime.fromisoformat(at) + timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
        with patch("tasks_core.timestamp", return_value=later):
            self.assertEqual(cues(), expected)
            first = board.action(avery, "my-ticket-cues")["items"][0]
            board.action(avery, "acknowledge-stalled-cue", {"id": recovery, "event": str(first["event_id"])})
            self.assertEqual(cues(), expected[1:])
