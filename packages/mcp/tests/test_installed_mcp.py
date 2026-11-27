"""Check the tool contract of an installed Taskboard adapter."""

import unittest
from unittest.mock import patch

from taskboard_client import SharedBoard
from taskboard_client.direct import DirectBoard
from taskboard_mcp import build_server


class InstalledMcpTest(unittest.IsolatedAsyncioTestCase):
    async def test_tool_contract(self):
        server = build_server(SharedBoard("tasks-board"))
        tools = {tool.name: tool for tool in await server.list_tools()}
        self.assertEqual(set(tools), {
            "taskboard_context", "taskboard_queue", "taskboard_ticket", "taskboard_reviews",
            "taskboard_start", "taskboard_update", "taskboard_done", "taskboard_reopen", "taskboard_create_project",
            "taskboard_assign_human", "taskboard_raise_attention",
            "taskboard_activity", "taskboard_create_ticket", "taskboard_edit_ticket", "taskboard_log",
            "taskboard_record_decision",
            "taskboard_projects", "taskboard_field_guidance", "taskboard_raft_channel", "taskboard_checkpoint",
            "taskboard_recurring_schedule", "taskboard_maintenance_schedule", "taskboard_goals", "taskboard_wake_subscriptions",
            "taskboard_knowledge_areas", "taskboard_knowledge_sweep", "taskboard_worker_checkpoint",
            "taskboard_project_access", "taskboard_set_project_access", "taskboard_agent_usage", "taskboard_api_usage",
            "taskboard_delegate", "taskboard_manual_review", "taskboard_ask_agent",
            "taskboard_save_standing_goal", "taskboard_standing_pass",
        })
        self.assertEqual(tools["taskboard_start"].input_schema["required"], ["ticket", "note"])
        self.assertTrue(tools["taskboard_context"].annotations.read_only_hint)
        self.assertTrue(tools["taskboard_activity"].annotations.read_only_hint)
        self.assertTrue(tools["taskboard_projects"].annotations.read_only_hint)
        self.assertTrue(tools["taskboard_field_guidance"].annotations.read_only_hint)
        self.assertTrue(tools["taskboard_raft_channel"].annotations.read_only_hint)
        self.assertEqual(tools["taskboard_raft_channel"].input_schema["required"], ["project"])
        self.assertFalse(tools["taskboard_done"].annotations.idempotent_hint)
        self.assertFalse(tools["taskboard_checkpoint"].annotations.idempotent_hint)
        self.assertEqual(tools["taskboard_checkpoint"].input_schema["required"], ["ticket", "note"])
        self.assertFalse(tools["taskboard_create_ticket"].annotations.idempotent_hint)

    async def test_record_decision_routes_choice_and_rationale(self):
        board = SharedBoard("tasks-board")
        args = {"ticket": "T-1", "note": "Use CSV", "details": "Keep it tabular. Update the export."}
        with patch.object(board, "invoke", return_value={"receipt": "T-1 · Decision"}) as invoke:
            await build_server(board).call_tool("taskboard_record_decision", args)
            invoke.assert_called_once_with("record-decision", {
                "id": "T-1", "note": args["note"], "details": args["details"],
            }, write=True)

    async def test_local_dri_tools_expose_project_actions_and_keep_the_default_queue(self):
        board = DirectBoard("https://example.test/tasks", local_dri="local-dri:11111111-1111-1111-1111-111111111111")
        tools = {tool.name for tool in await build_server(board).list_tools()}
        self.assertTrue({"taskboard_create_project", "taskboard_project_access", "taskboard_set_project_access",
                         "taskboard_agent_usage"} <= tools)
        self.assertTrue({"taskboard_context", "taskboard_projects", "taskboard_create_ticket", "taskboard_edit_ticket"} <= tools)
        with patch.object(board, "invoke", return_value={"items": [], "next_cursor": None}) as invoke:
            await build_server(board).call_tool("taskboard_queue", {})
            invoke.assert_called_once_with("my-queue", {"limit": 20, "state": "ready", "order": "priority"})


if __name__ == "__main__":
    unittest.main()
