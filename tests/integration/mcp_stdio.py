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
from urllib.parse import parse_qs, urlsplit

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
                    await self.verify_field_guidance(owner, reviewer, python, origin, cookie, blake_cookie,
                                                     avery_environment)
                    await self.verify_raft_channel(owner, reviewer, python, origin, cookie, blake_cookie,
                                                   avery_environment, blake_environment)
                    await self.verify_project_creation(owner, reviewer, python, origin, cookie, database, avery_environment)
                    await self.verify_ticket_tools(owner, reviewer, python, origin, cookie, blake_cookie,
                                                   avery_environment)
                    await self.verify_needs_you(owner, reviewer, python, origin, cookie, avery_environment)
                async with connect(with_lost_receipt(folder, avery_environment)) as owner:
                    lost = await owner.call_tool("taskboard_start", {"ticket": "T-1", "note": "Start the spare fixture."})
                    self.assertTrue(lost.is_error)
                    self.assertIn("Shared write outcome unknown", lost.content[0].text)
                history = io_action(origin, "task-activity", cookie=cookie, query={"id": "T-1"})
                self.assertEqual([event["action"] for event in history["items"]].count("start"), 1)

    async def test_configured_named_prompts_match_signed_http(self):
        python = Path(os.environ["TASKBOARD_MCP_FIXTURE_PYTHON"]).absolute()
        with TemporaryDirectory(prefix="taskboard-mcp-prompts-") as temporary:
            folder = Path(temporary)
            guidance = folder / "agent-guidance.toml"
            checkpoint = "Keep a backed-up fixture and record the reason it is representative."
            end_task = "Read back the published artifact before closing the ticket."
            knowledge = "Link the checked source before editing the handbook."
            guidance.write_text(
                f'[agent_guidance.prompts]\ncheckpoint = "{checkpoint}"\nend-task = "{end_task}"\n'
                f'knowledge = "{knowledge}"\n',
                encoding="utf-8",
            )
            with fixture_server(python, ROOT, folder / "service", *certificate(folder), guidance=guidance) as mounted:
                origin, cookie, _, environment = mounted
                signed = io_action(origin, "field-guidance", cookie=cookie)
                async with connect(environment) as owner:
                    result = await owner.call_tool("taskboard_field_guidance", {})
                    self.assertFalse(result.is_error, result.content)
                    self.assertEqual(result.structured_content, signed)
                self.assertEqual(signed["prompts"]["checkpoint"]["prompt"], checkpoint)
                self.assertEqual(signed["prompts"]["end-task"]["prompt"], end_task)
                self.assertEqual(signed["prompts"]["knowledge"], {"prompt": knowledge, "source": "board"})
                self.assertEqual({entry["source"] for entry in signed["prompts"].values()}, {"board"})
                self.assertIn("current_handoff", signed["prompts"]["checkpoint"]["example"])
                self.assertEqual(
                    {field["source"] for field in signed["fields"].values()}, {"default"},
                )

    async def verify_needs_you(self, owner, reviewer, python, origin, cookie, environment):
        with request(origin, MOUNT + "/auth/login", follow_redirects=False) as response:
            pending = response.headers["Set-Cookie"].split(";", 1)[0]
            state = parse_qs(urlsplit(response.headers["Location"]).query)["state"][0]
        with request(origin, MOUNT + "/auth/callback", cookie=pending, query={"code": "Sam", "state": state},
                     follow_redirects=False) as response:
            sam_cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        with request(origin, MOUNT + "/api/v1/session", cookie=sam_cookie) as response:
            self.assertEqual(json.load(response)["actor"]["name"], "Sam")
        created = io_action(origin, "create-task", cookie=cookie, data={
            "title": "Needs You fixture", "step": "Ask about the export.",
        })
        ticket = created["receipt"].split()[0]
        opened = {"ticket": ticket, "reason": "Review the export.", "recipient": "@Sam", "request_key": "one-ask"}
        rejected = await reviewer.call_tool("taskboard_raise_attention", opened)
        self.assertTrue(rejected.is_error)
        self.assertIn("Only the task owner", rejected.content[0].text)
        attention = await owner.call_tool("taskboard_raise_attention", opened)
        self.assertFalse(attention.is_error, attention.content)
        receipt = attention.structured_content
        self.assertIn(f"Attention request ID: {receipt['id']}", cli_success(
            python, ROOT, environment, "raise-attention", ticket, opened["reason"], "--on", "@Sam",
            "--request-key", "one-ask",
        ))
        signed = io_action(origin, "raise-attention", cookie=cookie, data={
            "id": ticket, "reason": opened["reason"], "recipient_name": "Sam", "request_key": "one-ask",
        })
        self.assertEqual(signed, receipt)
        with request(origin, MOUNT + "/api/v1/notifications/requests", cookie=sam_cookie) as response:
            requests = json.load(response)["items"]
        self.assertEqual([row["id"] for row in requests], [receipt["id"]])
        detail = io_action(origin, "task", cookie=cookie, query={"id": ticket})
        self.assertEqual(detail["task"]["state"], "ready")

        cli_success(python, ROOT, environment, "start", ticket, "Ask about the export.")
        question = {"ticket": ticket, "question": "Which format?", "recipient": "Sam", "options": ["CSV", "JSON"]}
        denied = await reviewer.call_tool("taskboard_assign_human", question)
        self.assertTrue(denied.is_error)
        sent = await owner.call_tool("taskboard_assign_human", question)
        self.assertFalse(sent.is_error, sent.content)
        repeated = await owner.call_tool("taskboard_assign_human", question)
        self.assertTrue(repeated.is_error)
        history = io_action(origin, "task-activity", cookie=cookie, query={"id": ticket})["items"]
        self.assertEqual([row["action"] for row in history].count("assign-human"), 1)
        with request(origin, MOUNT + "/api/v1/notifications/decisions", cookie=sam_cookie) as response:
            decision, = json.load(response)["items"]
        self.assertEqual((decision["note"], decision["context"]["choices"]), (question["question"], ["CSV", "JSON"]))

    async def verify_field_guidance(self, owner, reviewer, python, origin, cookie, blake_cookie, environment):
        async def compare(project=None):
            params = {"project": project} if project is not None else {}
            result = await owner.call_tool("taskboard_field_guidance", params)
            self.assertFalse(result.is_error, result.content)
            signed = io_action(origin, "field-guidance", cookie=cookie, query=params)
            self.assertEqual(result.structured_content, signed)
            prefix = project or "T"
            self.assertEqual(signed["project"], prefix)
            with request(origin, MOUNT + f"/api/v1/projects/{prefix}/field-guidance", cookie=cookie) as response:
                browser = json.load(response)
            self.assertEqual(browser, {key: signed[key] for key in ("project", "editable", "fields", "prompts")})
            cli_args = ("--project", project) if project is not None else ()
            printed = cli_success(python, ROOT, environment, "field-guidance", *cli_args)
            self.assertIn(f"{prefix} · Writing guidance", printed)
            for field in signed["fields"].values():
                for key in ("guidance", "where", "example", "format"):
                    self.assertIn(field[key], printed)
            return signed

        default = await compare()
        self.assertEqual({field["source"] for field in default["fields"].values()}, {"default"})
        self.assertIn("An unchanged idle check needs no write.", default["prompts"]["checkpoint"]["prompt"])
        self.assertIn("current_handoff", default["prompts"]["checkpoint"]["example"])
        self.assertIn("Before marking the ticket Done", default["prompts"]["end-task"]["prompt"])
        self.assertIn("linked sources", default["prompts"]["knowledge"]["prompt"])
        course = await compare("COURSE")
        self.assertTrue(course["editable"])
        self.assertEqual(course["fields"], default["fields"])
        path = MOUNT + "/api/v1/projects/COURSE/field-guidance"
        text = "Describe a concrete result in the course title."
        with request(origin, path, method="PUT", cookie=cookie,
                     data={"field": "title", "guidance": text}) as response:
            self.assertEqual(response.status, 200)
        updated = await compare("COURSE")
        self.assertEqual(updated["fields"], {
            **course["fields"], "title": {**course["fields"]["title"], "guidance": text, "source": "project"},
        })
        self.assertEqual(await compare(), default)

        prompt = "Keep the evidence and the follow-up in the course handbook."
        with request(origin, path, method="PUT", cookie=cookie,
                     data={"prompt": "knowledge", "guidance": prompt}) as response:
            self.assertEqual(response.status, 200)
        with_prompt = await compare("COURSE")
        self.assertEqual(with_prompt["prompts"], {
            **updated["prompts"], "knowledge": {"prompt": prompt, "source": "project"},
        })
        self.assertEqual(await compare(), default)
        other = await reviewer.call_tool("taskboard_field_guidance", {"project": "COURSE"})
        self.assertFalse(other.is_error, other.content)
        self.assertEqual(other.structured_content, io_action(
            origin, "field-guidance", cookie=blake_cookie, query={"project": "COURSE"},
        ))
        self.assertFalse(other.structured_content["editable"])
        self.assertEqual(other.structured_content["fields"], updated["fields"])
        self.assertEqual(other.structured_content["prompts"], with_prompt["prompts"])
        with request(origin, path, method="PUT", cookie=cookie,
                     data={"prompt": "knowledge", "guidance": None}) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(await compare("COURSE"), updated)

        unsigned_env = {**environment, "TASKBOARD_COMPAT_COOKIE": ""}
        async with connect(unsigned_env) as unsigned:
            for client, session, env, project, status in (
                (owner, cookie, environment, "MISSING", 404),
                (unsigned, "", unsigned_env, "COURSE", 401),
            ):
                params = {"project": project}
                with self.assertRaises(HTTPError) as blocked:
                    io_action(origin, "field-guidance", cookie=session, query=params)
                with blocked.exception as response:
                    self.assertEqual(response.code, status)
                    error = json.load(response)["error"]
                if status == 401:
                    error = "The Tasks app did not accept the current Raft session."
                rejected = await client.call_tool("taskboard_field_guidance", params)
                self.assertTrue(rejected.is_error)
                self.assertIn(error, rejected.content[0].text)
                denied = cli(python, ROOT, env, "field-guidance", "--project", project)
                self.assertNotEqual(denied.returncode, 0)
                self.assertIn(error, denied.stderr)

    async def verify_raft_channel(self, owner, reviewer, python, origin, cookie, blake_cookie,
                                  environment, blake_environment):
        params = {"project": "COURSE"}
        result = await owner.call_tool("taskboard_raft_channel", params)
        self.assertFalse(result.is_error, result.content)
        signed = io_action(origin, "raft-channel-show", cookie=cookie, query=params)
        self.assertEqual(result.structured_content, signed)
        value = {"project": "COURSE", "state": "unavailable", "channel": None}
        self.assertEqual({key: signed[key] for key in value}, value)
        with request(origin, MOUNT + "/api/v1/projects/COURSE/raft-channel", cookie=cookie) as response:
            self.assertEqual(json.load(response), value)
        output = cli_success(python, ROOT, environment, "raft-channel-show", "COURSE")
        self.assertIn("COURSE · Raft channel · Unavailable", output)

        with self.assertRaises(HTTPError) as blocked:
            io_action(origin, "raft-channel-show", cookie=blake_cookie, query=params)
        with blocked.exception as response:
            self.assertEqual(response.code, 403)
            error = json.load(response)["error"]
        rejected = await reviewer.call_tool("taskboard_raft_channel", params)
        self.assertTrue(rejected.is_error)
        self.assertIn(error, rejected.content[0].text)
        denied = cli(python, ROOT, blake_environment, "raft-channel-show", "COURSE")
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn(error, denied.stderr)

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

        private = await owner.call_tool("taskboard_create_project", {
            "prefix": "RINGLAB", "title": "Fictional restricted lab", "visibility": "ring1",
        })
        self.assertFalse(private.is_error, private.content)
        self.assertEqual(private.structured_content["project"]["visibility"], "ring1")
        ticket = await owner.call_tool("taskboard_create_ticket", {
            "project": "RINGLAB", "title": "Fictional specimen report", "step": "Record the sample result.",
        })
        self.assertFalse(ticket.is_error, ticket.content)
        key = ticket.structured_content["receipt"].split()[0]
        allowed = await owner.call_tool("taskboard_ticket", {"ticket": key})
        self.assertFalse(allowed.is_error, allowed.content)
        denied = await reviewer.call_tool("taskboard_ticket", {"ticket": key})
        self.assertTrue(denied.is_error)
        self.assertNotIn("Fictional specimen report", denied.content[0].text)
        outsider = await reviewer.call_tool("taskboard_projects")
        self.assertFalse(outsider.is_error, outsider.content)
        self.assertNotIn("RINGLAB", [item["prefix"] for item in outsider.structured_content["items"]])

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
        edited = await reviewer.call_tool("taskboard_edit_ticket", {"ticket": ticket, **correction})
        self.assertFalse(edited.is_error, edited.content)
        self.assertEqual(edited.structured_content["receipt"], f"{ticket} · Details updated")
        expected_actor = io_action(origin, "context", cookie=blake_cookie)["actor"]["id"]
        mcp_event, = io_action(origin, "task-activity", cookie=cookie, query={"id": ticket, "limit": 1})["items"]
        self.assertEqual(mcp_event["actor"], expected_actor)
        with request(origin, MOUNT + f"/api/v1/tasks/{ticket}/details", cookie=blake_cookie,
                     data={"brief": "Retain the signed response.", "reason": "Add evidence."}) as response:
            self.assertEqual(json.load(response)["receipt"], f"{ticket} · Details updated")
        output = cli_success(python, ROOT, {**environment, "TASKBOARD_COMPAT_COOKIE": blake_cookie},
                             "edit", ticket, "Clear the temporary brief.", "--brief", "")
        self.assertIn("Details updated", output)
        records = io_action(origin, "task-activity", cookie=cookie, query={"id": ticket, "limit": 3})["items"]
        self.assertEqual([event["actor"] for event in records], [expected_actor] * 3)
        self.assertEqual(records[0]["context"]["audit"]["previous"]["brief"], "Retain the signed response.")
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
        self.assertEqual(stored["task"]["dri"], io_action(origin, "context", cookie=cookie)["actor"]["id"])
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
        long_text = "## Results\n\n" + ("Keep a full **readable** paragraph.\n\n" * 130) + "End."
        fields = {"ticket": ticket, "reason": "Retain the full scope.", "brief": long_text}
        rejected = await owner.call_tool("taskboard_edit_ticket", fields)
        self.assertTrue(rejected.is_error)
        self.assertIn("force_text=true", rejected.content[0].text)
        self.assertEqual(io_action(origin, "task", cookie=cookie, query={"id": ticket})["brief"], "")
        saved = await owner.call_tool("taskboard_edit_ticket", {**fields, "force_text": True})
        self.assertFalse(saved.is_error, saved.content)
        self.assertEqual(io_action(origin, "task", cookie=cookie, query={"id": ticket})["brief"], long_text)
        checkpoint = await reviewer.call_tool("taskboard_checkpoint", {
            "ticket": ticket, "note": "Resume on the signed fixture.", "details": "Use the checked fixture because it is repeatable.",
            "label": "Fixture evidence", "url": "https://example.test/fixture",
        })
        self.assertFalse(checkpoint.is_error, checkpoint.content)
        self.assertEqual(checkpoint.structured_content["receipt"], f"{ticket} · Checkpoint")
        view = await owner.call_tool("taskboard_activity", {"ticket": ticket, "limit": 1})
        self.assertFalse(view.is_error, view.content)
        event, = view.structured_content["items"]
        self.assertEqual((event["action"], event["label"], event["actor"]), ("log", "Checkpoint", expected_actor))
        self.assertIs(event["context"]["checkpoint"], True)
        self.assertEqual(event["details"], "Use the checked fixture because it is repeatable.")
        self.assertEqual(io_action(origin, "task", cookie=cookie, query={"id": ticket})["task"], stored["task"])

        entry = io_action(origin, "create-knowledge", cookie=cookie, data={
            "project": "COURSE", "title": "Fixture guidance", "brief": "Review the planned outcome.",
        })["id"]
        edited = await reviewer.call_tool("taskboard_edit_ticket", {
            "ticket": entry, "brief": "Review the verified outcome.", "reason": "Record the checked result.",
        })
        self.assertFalse(edited.is_error, edited.content)
        self.assertEqual(edited.structured_content["receipt"], f"{entry} · Details updated")
        with request(origin, MOUNT + f"/api/v1/tasks/{entry}", cookie=blake_cookie) as response:
            stored = json.load(response)
        self.assertEqual(stored["task"]["record_kind"], "knowledge")
        self.assertEqual(stored["brief"], "Review the verified outcome.")
        event, = io_action(origin, "task-activity", cookie=cookie, query={"id": entry, "limit": 1})["items"]
        self.assertEqual(event["actor"], expected_actor)

    async def verify_workflow(self, owner, reviewer, python, origin, cookie, environment, blake):
        async def value(client, name, **params):
            result = await client.call_tool(name, params)
            self.assertFalse(result.is_error, result.content)
            return result.structured_content

        tools = {tool.name: tool for tool in (await owner.list_tools()).tools}
        self.assertEqual(set(tools), {
            "taskboard_context", "taskboard_queue", "taskboard_reviews", "taskboard_ticket",
            "taskboard_start", "taskboard_update", "taskboard_done", "taskboard_reopen", "taskboard_create_project",
            "taskboard_assign_human", "taskboard_raise_attention",
            "taskboard_activity", "taskboard_create_ticket", "taskboard_edit_ticket", "taskboard_log",
            "taskboard_projects", "taskboard_field_guidance", "taskboard_raft_channel", "taskboard_checkpoint",
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

        reopen = {"ticket": "T-2", "reason": "Check the corrected fixture."}
        warning = await reviewer.call_tool("taskboard_reopen", reopen)
        self.assertTrue(warning.is_error)
        self.assertIn("force_reason", warning.content[0].text)
        peer_reason = "The expected fixture was corrected."
        reopened = await value(reviewer, "taskboard_reopen", **reopen, force=True, force_reason=peer_reason)
        self.assertEqual(reopened["receipt"], "T-2 · Reopened")
        task = (await value(owner, "taskboard_ticket", ticket="T-2"))["task"]
        self.assertEqual((task["state"], task["dri"]), ("ready", details["task"]["dri"]))
        event, = io_action(origin, "task-activity", cookie=cookie, query={"id": "T-2", "limit": 1})["items"]
        self.assertEqual(event["actor"], blake["id"])
        self.assertEqual(event["context"]["transition_override"]["reason"], peer_reason)


if __name__ == "__main__":
    unittest.main()
