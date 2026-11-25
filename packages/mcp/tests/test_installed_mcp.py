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
            "taskboard_activity", "taskboard_create_ticket", "taskboard_edit_ticket", "taskboard_log",
            "taskboard_projects", "taskboard_field_guidance", "taskboard_raft_channel", "taskboard_checkpoint",
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

    async def test_local_dri_tools_respect_project_creation_boundary(self):
        board = DirectBoard("https://example.test/tasks", local_dri="local-dri:11111111-1111-1111-1111-111111111111")
        tools = {tool.name for tool in await build_server(board).list_tools()}
        self.assertNotIn("taskboard_create_project", tools)
        self.assertTrue({"taskboard_context", "taskboard_projects", "taskboard_create_ticket", "taskboard_edit_ticket"} <= tools)
        with patch.object(board, "invoke", return_value={"items": [], "next_cursor": None}) as invoke:
            await build_server(board).call_tool("taskboard_queue", {})
            invoke.assert_called_once_with("my-queue", {"limit": 20, "state": "ready", "order": "priority"})


if __name__ == "__main__":
    unittest.main()
