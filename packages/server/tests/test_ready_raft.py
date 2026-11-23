from hashlib import sha256
import json
import unittest
from unittest.mock import Mock

from taskboard_server.ready_raft import ReadyEventAcceptance, ReadyEventSendError, send_ready_event
from taskboard_server.ready_signal import signal


BOARD = "https://tasks.example.test/board"
TOKEN = "fixture-token-for-one-agent"
STAMP = "2026-11-22T18:00:00Z"


class ReadyRaftTest(unittest.TestCase):
    def setUp(self):
        self.signal = signal("fixture-agent", BOARD, 42, 12, STAMP)

    def test_posts_qualified_ready_fact_to_agent_events(self):
        transport = Mock(return_value=(202, {"status": "queued"}))
        accepted = send_ready_event(self.signal, access_token=TOKEN, transport=transport)

        transport.assert_called_once()
        request = transport.call_args.args[0]
        key = f"taskboard-ready-{sha256(BOARD.encode()).hexdigest()}-42"
        self.assertEqual(accepted, ReadyEventAcceptance(key, "queued"))
        self.assertEqual(request.full_url, "https://api.raft.build/api/oauth/agent-events")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data), {
            "kind": "event",
            "summary": f"Taskboard ticket T-12 is Ready in your owned queue. Ready event 42. {BOARD}/#task-12",
            "externalEventId": key,
        })

    def test_retry_preserves_the_whole_request_and_accepts_dedup(self):
        transport = Mock(side_effect=[TimeoutError(TOKEN), (200, {"deduped": True})])
        with self.assertRaises(ReadyEventSendError) as raised:
            send_ready_event(self.signal, access_token=TOKEN, transport=transport)
        self.assertNotIn(TOKEN, str(raised.exception))
        self.assertIsNone(raised.exception.__context__)
        self.assertEqual(transport.call_count, 1)

        accepted = send_ready_event(self.signal, access_token=TOKEN, transport=transport)
        first, second = (call.args[0] for call in transport.call_args_list)
        self.assertEqual(first.data, second.data)
        self.assertEqual(accepted, ReadyEventAcceptance(json.loads(first.data)["externalEventId"], "deduped"))

    def test_board_and_source_event_scope_the_external_key(self):
        transport = Mock(return_value=(202, {"status": "queued"}))
        sources = [
            self.signal,
            signal("fixture-agent", BOARD + "-other", 42, 12, STAMP),
            signal("fixture-agent", BOARD, 43, 12, STAMP),
        ]
        keys = {send_ready_event(source, access_token=TOKEN, transport=transport).external_event_id
                for source in sources}
        self.assertEqual(len(keys), len(sources))
        self.assertTrue(all(len(key) <= 200 for key in keys))

    def test_non_acceptance_responses_do_not_supply_a_receipt(self):
        for status, response in [
            (202, {"status": "delivered"}), (202, None), (200, {"status": "queued"}),
            (200, {"deduped": 1}), (200, {"deduped": False}), (302, {"status": "queued"}),
            (401, {"error": TOKEN}), (503, {"deduped": True}),
        ]:
            with self.subTest(status=status, response=response), self.assertRaises(ReadyEventSendError) as raised:
                send_ready_event(self.signal, access_token=TOKEN, transport=Mock(return_value=(status, response)))
            self.assertIn(f"HTTP {status}", str(raised.exception))
            self.assertNotIn(TOKEN, str(raised.exception))

    def test_rejects_unusable_input_before_transport(self):
        transport = Mock()
        for override in [
            {"event_id": True}, {"task_url": BOARD + "/#task-13"},
            {"task_url": "http://tasks.example.test/#task-12"},
            {"task_url": "https://secret@tasks.example.test/#task-12"},
            {"task_url": BOARD + "/new\nline/#task-12"},
            {"task_url": BOARD + "/" + "x" * 2000 + "/#task-12"},
        ]:
            with self.subTest(override=override), self.assertRaises(ValueError):
                send_ready_event(self.signal | override, access_token=TOKEN, transport=transport)
        with self.assertRaises(ValueError):
            send_ready_event(self.signal, access_token=TOKEN + "\r\n", transport=transport)
        transport.assert_not_called()
