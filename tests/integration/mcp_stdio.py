"""Exercise the Taskboard adapter with an official MCP client and a mounted fixture."""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from urllib.error import HTTPError

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/release"))
from compatibility_fixture import MOUNT, cli, cli_success, fixture_server, io_action, request, seed
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
            with fixture_server(python, ROOT, folder / "service", *certificate(folder), project_admin=True) as mounted:
                origin, cookie, database, avery_environment = mounted
                seed(python, ROOT, origin, cookie, database)
                with request(origin, MOUNT + "/auth/callback", query={"code": "Blake"}) as response:
                    blake_cookie = response.headers["Set-Cookie"].split(";", 1)[0]
                blake_environment = {**avery_environment, "TASKBOARD_COMPAT_COOKIE": blake_cookie}
                blake = io_action(origin, "context", cookie=blake_cookie)["actor"]
                async with connect(avery_environment) as owner, connect(blake_environment) as reviewer:
                    await self.verify_workflow(owner, reviewer, python, origin, cookie, avery_environment, blake)
                    await self.verify_project_creation(owner, reviewer, python, origin, cookie, database, avery_environment)
                    await self.verify_ticket_tools(owner, reviewer, python, origin, cookie, blake_cookie,
                                                   avery_environment)
                async with connect(with_lost_receipt(folder, avery_environment)) as owner:
                    lost = await owner.call_tool("taskboard_start", {"ticket": "T-1", "note": "Start the spare fixture."})
                    self.assertTrue(lost.is_error)
                    self.assertIn("Shared write outcome unknown", lost.content[0].text)
                history = io_action(origin, "task-activity", cookie=cookie, query={"id": "T-1"})
                self.assertEqual([event["action"] for event in history["items"]].count("start"), 1)

    async def verify_project_creation(self, owner, reviewer, python, origin, cookie, database, environment):
        actor = io_action(origin, "context", cookie=cookie)["actor"]
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("INSERT INTO project_admin_grants VALUES (?)", (actor["id"],))

        params = {"prefix": "LAB", "title": "Fictional Lab"}
        denied = await reviewer.call_tool("taskboard_create_project", params)
        self.assertTrue(denied.is_error)
        self.assertIn("Taskboard project creation permission is required.", denied.content[0].text)

        created = await owner.call_tool("taskboard_create_project", params)
        self.assertFalse(created.is_error, created.content)
        project = {**params, "dri": actor["id"]}
        self.assertEqual(created.structured_content["project"], project)
        with request(origin, MOUNT + "/api/v1/projects", data=params, cookie=cookie) as response:
            self.assertEqual(json.load(response)["project"], project)
        self.assertIn("Project LAB · Registered", cli_success(
            python, ROOT, environment, "create-project", "LAB", "Fictional Lab",
        ))
        with request(origin, MOUNT + "/api/v1/projects") as response:
            self.assertEqual([row["prefix"] for row in json.load(response)["items"]].count("LAB"), 1)
        listed = await owner.call_tool("taskboard_projects")
        self.assertFalse(listed.is_error, listed.content)
        directory = listed.structured_content
        self.assertEqual(directory, json.loads(cli_success(python, ROOT, environment, "projects", "--json")))
        with request(origin, MOUNT + "/api/v1/projects", cookie=cookie) as response:
            self.assertEqual({key: directory[key] for key in ("default_project", "items")}, json.load(response))
        self.assertEqual(directory["default_project"], "T")
        self.assertEqual([row["prefix"] for row in directory["items"]], ["T", "COURSE", "LAB"])
        human_output = cli_success(python, ROOT, environment, "projects")
        self.assertIn("Default project: T", human_output)
        self.assertIn("LAB · Fictional Lab", human_output)

    async def verify_ticket_tools(self, owner, reviewer, python, origin, cookie, blake_cookie, environment):
        created = await owner.call_tool("taskboard_create_ticket", {
            "project": "COURSE", "title": "Check a fixture outcome",
            "step": "Read the signed response.", "brief": "Compare the output.",
            "label": "Fixture specification", "url": "https://example.test/specification",
        })
        self.assertFalse(created.is_error, created.content)
        receipt = created.structured_content["receipt"]
        ticket = receipt.split()[0]
        self.assertEqual(ticket, "COURSE-2")
        self.assertIn("created", receipt)
        self.assertIn("COURSE-2 · Ready", cli_success(python, ROOT, environment, "show", ticket))

        correction = {"title": "Verify the fixture outcome", "brief": "", "reason": "Clarify scope."}
        rejected = await reviewer.call_tool("taskboard_edit_ticket", {"ticket": ticket, **correction})
        self.assertTrue(rejected.is_error)
        with self.assertRaises(HTTPError) as blocked:
            request(origin, MOUNT + f"/api/v1/tasks/{ticket}/details", cookie=blake_cookie, data=correction)
        with blocked.exception as response:
            self.assertEqual(response.code, 403)
            error = json.load(response)["error"]
        self.assertIn(error, rejected.content[0].text)
        denied_cli = cli(python, ROOT, {**environment, "TASKBOARD_COMPAT_COOKIE": blake_cookie},
                         "edit", ticket, correction["reason"], "--title", correction["title"])
        self.assertNotEqual(denied_cli.returncode, 0)
        self.assertIn(error, denied_cli.stderr)

        edited = await owner.call_tool("taskboard_edit_ticket", {"ticket": ticket, **correction})
        self.assertFalse(edited.is_error, edited.content)
        self.assertEqual(edited.structured_content["receipt"], f"{ticket} · Details updated")
        logged = await owner.call_tool("taskboard_log", {
            "ticket": ticket, "note": "Compare fixture responses", "details": "The records agree.",
            "label": "Fixture readback", "url": "https://example.test/readback",
        })
        self.assertFalse(logged.is_error, logged.content)
        self.assertEqual(logged.structured_content["receipt"], f"{ticket} · Activity logged")

        with request(origin, MOUNT + f"/api/v1/tasks/{ticket}", cookie=cookie) as response:
            stored = json.load(response)
        self.assertEqual(stored["task"]["title"], correction["title"])
        self.assertEqual(stored["task"]["state"], "ready")
        self.assertEqual(stored["brief"], "")
        page = await owner.call_tool("taskboard_activity", {"ticket": ticket, "limit": 1})
        self.assertFalse(page.is_error, page.content)
        recent = page.structured_content
        expected = io_action(origin, "task-activity", cookie=cookie, query={"id": ticket, "limit": 1})
        self.assertEqual(recent, expected)
        self.assertEqual(recent["items"][0]["details"], "The records agree.")
        older = await owner.call_tool("taskboard_activity", {
            "ticket": ticket, "limit": 1, "before": recent["next_cursor"],
        })
        self.assertFalse(older.is_error, older.content)
        self.assertLess(older.structured_content["items"][0]["id"], recent["items"][0]["id"])
        shown = cli_success(python, ROOT, environment, "show", ticket)
        self.assertIn("Compare fixture responses", shown)
        self.assertIn("https://example.test/readback", shown)

    async def verify_workflow(self, owner, reviewer, python, origin, cookie, environment, blake):
        async def value(client, name, **params):
            result = await client.call_tool(name, params)
            self.assertFalse(result.is_error, result.content)
            return result.structured_content

        tools = {tool.name: tool for tool in (await owner.list_tools()).tools}
        self.assertEqual(set(tools), {
            "taskboard_context", "taskboard_queue", "taskboard_reviews", "taskboard_ticket",
            "taskboard_start", "taskboard_update", "taskboard_done", "taskboard_create_project",
            "taskboard_activity", "taskboard_create_ticket", "taskboard_edit_ticket", "taskboard_log",
            "taskboard_projects",
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
