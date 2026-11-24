"""Check the tool contract of an installed Taskboard adapter."""

import unittest

from taskboard_client import SharedBoard
from taskboard_mcp import build_server


class InstalledMcpTest(unittest.IsolatedAsyncioTestCase):
    async def test_tool_contract(self):
        server = build_server(SharedBoard("tasks-board"))
        tools = {tool.name: tool for tool in await server.list_tools()}
        self.assertEqual(set(tools), {
            "taskboard_context", "taskboard_queue", "taskboard_ticket", "taskboard_reviews",
            "taskboard_start", "taskboard_update", "taskboard_done", "taskboard_create_project",
        })
        self.assertEqual(tools["taskboard_start"].input_schema["required"], ["ticket", "note"])
        self.assertTrue(tools["taskboard_context"].annotations.read_only_hint)
        self.assertFalse(tools["taskboard_done"].annotations.idempotent_hint)


if __name__ == "__main__":
    unittest.main()
