"""Exercise the Taskboard adapter with an official MCP client and a mounted fixture."""

import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/release"))
from compatibility_fixture import MOUNT, cli_success, fixture_server, io_action, request, seed
from release_compatibility import certificate


def connect(environment):
    process = StdioServerParameters(
        command="uv", args=["run", "--locked", "--script", str(ROOT / "tasks_mcp.py")],
        cwd=ROOT, env=environment,
    )
    return Client(stdio_client(process), read_timeout_seconds=30)


def with_lost_receipt(folder, environment):
    bin_dir = folder / "lost-receipt"
    bin_dir.mkdir()
    shim = bin_dir / "raft"
    shim.write_text(f"""#!{sys.executable}
import os
import subprocess
import sys
arguments = sys.argv[1:]
payload = sys.stdin.buffer.read() if '--data-file' in arguments else None
result = subprocess.run([os.environ['TASKBOARD_TEST_RAFT'], *arguments],
                        input=payload, capture_output=True)
name = arguments[arguments.index('--action') + 1]
if name == 'advance-task':
    sys.exit(1)
sys.stdout.buffer.write(result.stdout)
sys.stderr.buffer.write(result.stderr)
sys.exit(result.returncode)
""")
    shim.chmod(0o700)
    return {
        **environment, "PATH": os.pathsep.join((str(bin_dir), environment["PATH"])),
        "TASKBOARD_TEST_RAFT": str(folder / "service/raft"),
    }


class TaskboardStdioTest(unittest.IsolatedAsyncioTestCase):
    async def test_two_agent_workflow(self):
        # The HTTP fixture uses the application dependencies while the tool process uses its own locked SDK.
        python = Path(os.environ["TASKBOARD_MCP_FIXTURE_PYTHON"]).absolute()
        with TemporaryDirectory(prefix="taskboard-mcp-") as temporary:
            folder = Path(temporary)
            with fixture_server(python, ROOT, folder / "service", *certificate(folder)) as mounted:
                origin, cookie, database, avery_environment = mounted
                seed(python, ROOT, origin, cookie, database)
                with request(origin, MOUNT + "/auth/callback", query={"code": "Blake"}) as response:
                    blake_cookie = response.headers["Set-Cookie"].split(";", 1)[0]
                blake_environment = {**avery_environment, "TASKBOARD_COMPAT_COOKIE": blake_cookie}
                blake = io_action(origin, "context", cookie=blake_cookie)["actor"]
                async with connect(avery_environment) as owner, connect(blake_environment) as reviewer:
                    await self.verify_workflow(owner, reviewer, python, origin, cookie, avery_environment, blake)
                async with connect(with_lost_receipt(folder, avery_environment)) as owner:
                    lost = await owner.call_tool("taskboard_start", {"ticket": "T-1", "note": "Start the spare fixture."})
                    self.assertTrue(lost.is_error)
                    self.assertIn("Shared write outcome unknown", lost.content[0].text)
                history = io_action(origin, "task-activity", cookie=cookie, query={"id": "T-1"})
                self.assertEqual([event["action"] for event in history["items"]].count("start"), 1)

    async def verify_workflow(self, owner, reviewer, python, origin, cookie, environment, blake):
        async def value(client, name, **params):
            result = await client.call_tool(name, params)
            self.assertFalse(result.is_error, result.content)
            return result.structured_content

        tools = {tool.name: tool for tool in (await owner.list_tools()).tools}
        self.assertEqual(set(tools), {
            "taskboard_context", "taskboard_queue", "taskboard_reviews", "taskboard_ticket",
            "taskboard_start", "taskboard_update", "taskboard_done",
        })
        self.assertEqual(tools["taskboard_start"].input_schema["required"], ["ticket", "note"])
        self.assertTrue(tools["taskboard_queue"].annotations.read_only_hint)
        self.assertFalse(tools["taskboard_done"].annotations.idempotent_hint)
        self.assertEqual((await value(owner, "taskboard_context"))["actor"]["name"], "Avery")
        self.assertEqual((await value(reviewer, "taskboard_context"))["actor"]["id"], blake["id"])

        taskboard = await value(owner, "taskboard_queue", project="T", limit=1)
        self.assertEqual(taskboard, io_action(origin, "my-queue", cookie=cookie, query={
            "project": "T", "limit": 1, "state": "ready", "order": "priority",
        }))
        self.assertEqual([row["key"] for row in taskboard["items"]], ["T-2"])
        page_two = await value(owner, "taskboard_queue", project="T", limit=1, before=taskboard["next_cursor"])
        self.assertEqual([row["key"] for row in page_two["items"]], ["T-1"])
        by_recency = await value(owner, "taskboard_queue", project="T", state=None, limit=1)
        self.assertIsInstance(by_recency["next_cursor"], int)
        older = await value(owner, "taskboard_queue", project="T", state=None, limit=1, before=by_recency["next_cursor"])
        self.assertEqual({row["key"] for row in by_recency["items"] + older["items"]}, {"T-1", "T-2"})
        course = await value(owner, "taskboard_queue", project="COURSE")
        self.assertEqual([row["key"] for row in course["items"]], ["COURSE-1"])
        self.assertIn("COURSE-1", cli_success(python, ROOT, environment, "next", "--project", "COURSE"))
        self.assertEqual((await value(reviewer, "taskboard_queue", project="COURSE"))["items"], [])

        rejected = await reviewer.call_tool("taskboard_start", {"ticket": "T-2", "note": "Wrong owner."})
        self.assertTrue(rejected.is_error)
        self.assertIn("write rejected", rejected.content[0].text)
        started = await value(owner, "taskboard_start", ticket="T-2", note="Compare the fixture.")
        self.assertEqual(started["receipt"], "T-2 · Started")
        self.assertIn("--eta", started["next_step"])
        for ticket in ("T-2", "T-1"):
            io_action(origin, "delegate", cookie=cookie, data={
                "id": ticket, "role": "review", "worker": blake["id"], "brief": "Verify the fixture.",
            })
        assigned = await value(reviewer, "taskboard_reviews", project="T", limit=1)
        self.assertEqual([row["task_key"] for row in assigned["items"]], ["T-1"])
        self.assertIsInstance(assigned["next_cursor"], int)
        earlier = await value(reviewer, "taskboard_reviews", project="T", limit=1, before=assigned["next_cursor"])
        self.assertEqual([row["task_key"] for row in earlier["items"]], ["T-2"])
        self.assertEqual((await value(reviewer, "taskboard_reviews", project="COURSE"))["items"], [])
        updated = await value(owner, "taskboard_update", ticket="T-2", note="Readback passed.")
        self.assertEqual(updated["receipt"], "T-2 · Progress")
        self.assertIn("--eta", updated["next_step"])
        done = await value(owner, "taskboard_done", ticket="T-2", note="The fixture passed.", details="Fixture proof.")
        self.assertEqual(done["receipt"], "T-2 · Completed")
        self.assertIn("Inbox", done["next_step"])
        details = await value(owner, "taskboard_ticket", ticket="T-2")
        self.assertEqual(details, io_action(origin, "task", cookie=cookie, query={"id": "T-2"}))
        self.assertEqual(details["task"]["state"], "done")
        self.assertIn("T-2 · Done", cli_success(python, ROOT, environment, "show", "T-2"))
        self.assertEqual((await value(owner, "taskboard_ticket", ticket="COURSE-1"))["task"]["state"], "ready")


if __name__ == "__main__":
    unittest.main()
