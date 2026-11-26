"""Exercise the Taskboard adapter with an official MCP client and a mounted fixture."""

from contextlib import closing
from datetime import datetime, timedelta
from hashlib import sha256
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
from compatibility_fixture import MOUNT, cli, cli_success, fixture_server, io_action, read_json, request, seed
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


def human_session(origin, name):
    with request(origin, MOUNT + "/auth/login", follow_redirects=False) as response:
        pending = response.headers["Set-Cookie"].split(";", 1)[0]
        state = parse_qs(urlsplit(response.headers["Location"]).query)["state"][0]
    with request(origin, MOUNT + "/auth/callback", cookie=pending, query={"code": name, "state": state},
                 follow_redirects=False) as response:
        cookie = response.headers["Set-Cookie"].split(";", 1)[0]
    read_json(origin, MOUNT + "/api/v1/session", cookie=cookie)
    return cookie


def local_reader(origin, environment, folder, public_cert, sam):
    site = MOUNT + "/api/v1/personal-agents/"
    origin_header = {"Origin": origin}
    local_dri = read_json(origin, site + "local-dris", cookie=sam, headers=origin_header,
                          data={"name": "Local reader", "project": "COURSE"})["local_dri"]["id"]
    token = read_json(origin, site + "connections", cookie=sam, headers=origin_header,
                      data={"session": "Readback fixture"})["token"]
    config = folder / "config"
    sessions = config / "taskboard" / "sessions"
    sessions.mkdir(mode=0o700, parents=True)
    saved = sessions / (sha256((origin + MOUNT).encode()).hexdigest() + ".json")
    saved.write_text(json.dumps({"token": token}))
    saved.chmod(0o600)
    return ({**environment, "TASKBOARD_URL": origin + MOUNT, "TASKBOARD_LOCAL_DRI": local_dri,
             "XDG_CONFIG_HOME": str(config), "SSL_CERT_FILE": str(public_cert)},
            {"Authorization": f"Bearer {token}", "X-Taskboard-Local-DRI": local_dri})


class TaskboardStdioTest(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_readers_see_saved_ticket_fields(self):
        python = Path(os.environ["TASKBOARD_MCP_FIXTURE_PYTHON"]).absolute()
        with TemporaryDirectory(prefix="taskboard-mcp-readback-") as temporary:
            folder = Path(temporary)
            public_cert, private_key = certificate(folder)
            with fixture_server(python, ROOT, folder / "service", public_cert, private_key,
                                project_admin=True, direct_reads=True) as mounted:
                origin, cookie, database, environment = mounted
                seed(python, ROOT, origin, cookie, database)
                ticket, path = "COURSE-1", MOUNT + "/api/v1/tasks/COURSE-1"
                day = "2099-01-05"
                io_action(origin, "edit-task-details", cookie=cookie, data={
                    "id": ticket, "brief": "Keep the whole fixture brief.", "reason": "Check stored text.",
                })
                io_action(origin, "edit-checklist", cookie=cookie, data={"id": ticket, "action": "add", "label": "Check receipt"})
                io_action(origin, "task-tag", cookie=cookie, data={"id": ticket, "action": "add", "tag": "fixture"})
                io_action(origin, "configure-ticket-fields", cookie=cookie, data={"project": "COURSE", "fields": json.dumps([
                    {"key": "effort", "label": "Effort", "kind": "duration", "prompt": "Read the fixture evidence."},
                ])})
                io_action(origin, "set-ticket-field", cookie=cookie, data={
                    "id": ticket, "field": "effort", "method": "measured", "coverage": "complete",
                    "value": "120", "basis": "A two-minute sample.", "url": "https://example.test/sample",
                })
                io_action(origin, "advance-task", cookie=cookie, data={
                    "id": ticket, "transition": "start", "note": "Check the fixture.",
                })
                io_action(origin, "advance-task", cookie=cookie, data={
                    "id": ticket, "transition": "update", "note": "Check the fixture.",
                    "eta": "2099-01-04T18:00:00Z", "started": "[1]",
                })
                io_action(origin, "advance-task", cookie=cookie, data={
                    "id": ticket, "transition": "park", "note": "Wait for the sample.", "review_after": day,
                    "review_on": "T-1", "review_criteria": "Resume after the sample is checked.",
                })

                # These readers sign in after the initial write.
                with request(origin, MOUNT + "/auth/callback", query={"code": "Blake"}) as response:
                    blake = response.headers["Set-Cookie"].split(";", 1)[0]
                team_env = {**environment, "TASKBOARD_COMPAT_COOKIE": blake}
                sam = human_session(origin, "Sam")
                local_env, signed_local = local_reader(origin, environment, folder, public_cert, sam)
                local_args = ("--local-dri", local_env["TASKBOARD_LOCAL_DRI"])
                io_action(origin, "watch-task", cookie=blake, data={"id": ticket})

                async def verify(expected):
                    for env, credentials, browser_cookie, prefix, watching in (
                        (team_env, {"cookie": blake}, blake, (), True),
                        (local_env, {"headers": signed_local}, sam, local_args, False),
                    ):
                        signed = read_json(origin, MOUNT + "/api/v1/raft/task", query={"id": ticket}, **credentials)
                        browser = read_json(origin, path, cookie=browser_cookie)
                        self.assertEqual(browser["task"], signed["task"])
                        self.assertEqual(browser["watching"], watching)
                        self.assertEqual(signed["task"]["reminder"], expected)
                        self.assertEqual(signed["task"]["eta"], "2099-01-04T18:00:00Z")
                        self.assertEqual(signed["task"]["priority"], "high")
                        self.assertEqual(signed["task"]["tags"], ["fixture"])
                        self.assertEqual(signed["brief"], "Keep the whole fixture brief.")
                        self.assertEqual(signed["checklist"][0]["label"], "Check receipt")
                        field, = signed["ticket_fields"]["fields"]
                        self.assertEqual((field["value"], field["basis"]), ("120", "A two-minute sample."))
                        self.assertEqual(json.loads(cli_success(python, ROOT, env, *prefix, "show", ticket, "--json")), signed)
                        async with connect(env) as reader:
                            detail = await reader.call_tool("taskboard_ticket", {"ticket": ticket})
                            self.assertFalse(detail.is_error, detail.content)
                            self.assertEqual(detail.structured_content, signed)
                            history = await reader.call_tool("taskboard_activity", {"ticket": ticket, "limit": 1})
                            self.assertFalse(history.is_error, history.content)
                            activity = read_json(origin, MOUNT + "/api/v1/raft/task-activity",
                                                 query={"id": ticket, "limit": 1}, **credentials)
                            self.assertEqual(history.structured_content, activity)
                            context = activity["items"][0]["context"]
                            self.assertEqual(context.get("review_after"), expected and expected.get("date"))
                            self.assertEqual(context.get("defer_until"), expected and expected.get("at"))

                await verify({"kind": "date", "date": day})
                self.assertIn(f"Review on or after {day}", cli_success(python, ROOT, team_env, "show", ticket))
                read_json(origin, path + "/reminder", cookie=cookie, method="PUT",
                          data={"at": "2099-01-06T10:30", "time_zone": "America/Los_Angeles"})
                await verify({"kind": "exact", "at": "2099-01-06T18:30:00Z"})
                self.assertIn("Deferred until 2099-01-06", cli_success(python, ROOT, local_env, *local_args, "show", ticket))
                cli_success(python, ROOT, environment, "remind", ticket, "--clear")
                await verify(None)
                shared = json.loads(cli_success(python, ROOT, local_env, *local_args, "show", "T-1", "--json"))
                self.assertEqual(shared["task"]["key"], "T-1")
                async with connect(local_env) as reader:
                    other_project = await reader.call_tool("taskboard_ticket", {"ticket": "T-1"})
                    self.assertFalse(other_project.is_error, other_project.content)
                    self.assertEqual(other_project.structured_content, shared)
                with self.assertRaises(HTTPError) as denied_browser:
                    read_json(origin, path, headers=signed_local)
                self.assertEqual(denied_browser.exception.code, 403)

                read_json(origin, MOUNT + "/api/v1/projects", cookie=sam, headers={"Origin": origin}, data={
                    "prefix": "LAB", "title": "Private fixture", "visibility": "ring1",
                })
                avery = io_action(origin, "context", cookie=cookie)["actor"]["id"]
                read_json(origin, MOUNT + "/api/v1/projects/LAB/ring1", cookie=sam, headers={"Origin": origin},
                          method="PUT", data={"principal": avery, "allowed": True})
                private = io_action(origin, "create-task", cookie=cookie, data={
                    "project": "LAB", "title": "Private ticket", "step": "Read the fixture.",
                })["receipt"].split()[0]
                io_action(origin, "advance-task", cookie=cookie, data={
                    "id": private, "transition": "park", "note": "Wait for the private sample.", "review_after": day,
                })
                self.assertEqual(io_action(origin, "task", cookie=cookie, query={"id": private})["task"]["reminder"],
                                 {"kind": "date", "date": day})
                with self.assertRaises(HTTPError) as hidden:
                    io_action(origin, "task", cookie=blake, query={"id": private})
                self.assertEqual(hidden.exception.code, 404)
                for env in (team_env, local_env):
                    async with connect(env) as reader:
                        self.assertTrue((await reader.call_tool("taskboard_ticket", {"ticket": private})).is_error)
                        self.assertTrue((await reader.call_tool("taskboard_activity", {"ticket": private})).is_error)
                local_id = signed_local["X-Taskboard-Local-DRI"]
                async with connect(local_env) as reader:
                    payload = {"prefix": "LOCAL", "title": "Fictional Local project"}
                    self.assertTrue((await reader.call_tool("taskboard_create_project", payload)).is_error)
                    read_json(origin, MOUNT + "/api/v1/admin/project-creation", cookie=sam,
                              method="PUT", headers={"Origin": origin}, data={"principal": local_id, "allowed": True})
                    created = await reader.call_tool("taskboard_create_project", payload)
                    self.assertFalse(created.is_error, created.content)
                    self.assertEqual(created.structured_content["project"]["dri"], local_id)
                    params = {"project": "LAB", "role": "ring1"}
                    self.assertTrue((await reader.call_tool("taskboard_project_access", params)).is_error)
                    for path in ("ring1", "owners"):
                        read_json(origin, MOUNT + f"/api/v1/projects/LAB/{path}", cookie=sam,
                                  method="PUT", headers={"Origin": origin}, data={"principal": local_id, "allowed": True})
                    members = await reader.call_tool("taskboard_project_access", params)
                    self.assertFalse(members.is_error, members.content)
                    self.assertIn(local_id, {member["id"] for member in members.structured_content["members"]})
                    updated = await reader.call_tool("taskboard_set_project_access", {
                        **params, "principal": avery, "allowed": False,
                    })
                    self.assertFalse(updated.is_error, updated.content)
                    self.assertNotIn(avery, {member["id"] for member in updated.structured_content["members"]})
                    usage = await reader.call_tool("taskboard_agent_usage", {})
                    self.assertFalse(usage.is_error, usage.content)
                    self.assertEqual(usage.structured_content["scope"], "board")
                    source = await reader.call_tool("taskboard_create_ticket", {
                        "project": "LOCAL", "title": "Verify a pinned result", "step": "Record the signed review.",
                    })
                    self.assertFalse(source.is_error, source.content)
                    key = source.structured_content["receipt"].split()[0]
                    pull = "https://github.com/example/fixture/pull/3"
                    with request(origin, MOUNT + "/api/v1/raft/link-github", headers={
                        **signed_local, "Origin": origin,
                    }, data={"id": key, "github_url": pull}):
                        pass
                    review = await reader.call_tool("taskboard_manual_review", {
                        "ticket": key, "change": "create", "note": "Review the fixture.",
                        "pull": pull, "head": "a" * 40, "base": "b" * 40, "recipient": avery,
                    })
                    self.assertFalse(review.is_error, review.content)
                    source = io_action(origin, "create-task", cookie=cookie, data={
                        "project": "COURSE", "title": "Review the second fixture", "step": "Verify the linked result.",
                    })["receipt"].split()[0]
                    io_action(origin, "link-github", cookie=cookie, data={"id": source, "github_url": pull})
                    pin = {"review_pr": pull, "review_head": "c" * 40, "review_base": "d" * 40}
                    receipt = io_action(origin, "peer-ask", cookie=cookie, data={
                        "id": source, "transition": "manual-create", "note": "Check the fixture.",
                        "recipient": local_id, **pin,
                    })["receipt"]
                    ask = receipt.split()[0]
                    started = await reader.call_tool("taskboard_start", {"ticket": ask, "note": "Check the fixture."})
                    self.assertFalse(started.is_error, started.content)
                    outcome = await reader.call_tool("taskboard_manual_review", {
                        "ticket": ask, "change": "result", "note": "Verified the fictional result.",
                        "pull": pull, "head": pin["review_head"], "base": pin["review_base"],
                        "url": pull, "label": "Fictional evidence",
                    })
                    self.assertFalse(outcome.is_error, outcome.content)
                    self.assertIn("Ready for acknowledgment", outcome.structured_content["receipt"])

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
                    await self.verify_saved_project_fields(owner, reviewer, python, origin, cookie, avery_environment)
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
        sam_cookie = human_session(origin, "Sam")
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

    async def verify_saved_project_fields(self, owner, reviewer, python, origin, cookie, environment):
        def action(name, data=None, **query):
            return io_action(origin, name, cookie=cookie, data=data, query=query)

        async def compare(tool, name, query, **params):
            expected = action(name, **query)
            result = await owner.call_tool(tool, params)
            self.assertFalse(result.is_error, result.content)
            actual = result.structured_content
            if name == "recurring-show":
                for report in (actual, expected):
                    self.assertEqual(datetime.fromisoformat(report["as_of"]).utcoffset(), timedelta(0))
                self.assertEqual({key: value for key, value in actual.items() if key != "as_of"},
                                 {key: value for key, value in expected.items() if key != "as_of"})
            else:
                self.assertEqual(actual, expected)
            return expected

        project = {"project": "COURSE"}
        first = "2099-01-05T18:30:00Z"
        recurrence = action("recurring-add", {**project, "title": "Check the next sample", "key": "sample-check",
                                              "step": "Read the signed sample.", "cadence_hours": 24, "next_at": first})
        schedule = recurrence["receipt"].split()[0]
        definition = await compare("taskboard_recurring_schedule", "recurring-show", {"id": schedule}, schedule=schedule)
        self.assertEqual((definition["step"], definition["first_at"], definition["next_at"]),
                         ("Read the signed sample.", first, first))
        shown = json.loads(cli_success(python, ROOT, environment, "recurring-show", schedule))
        self.assertEqual(shown["step"], definition["step"])
        browser = read_json(origin, MOUNT + "/api/v1/schedules/" + schedule, cookie=cookie, query=project)
        self.assertEqual(browser["schedule"]["step"], definition["step"])
        self.assertTrue((await reviewer.call_tool("taskboard_recurring_schedule", {"schedule": schedule})).is_error)

        actor = action("context")["actor"]["id"]
        action("maintenance-update", {**project, "owner": actor, "cadence_hours": 24, "next_at": first,
                                      "members": json.dumps([actor]), "state": "active"})
        maintenance = await compare("taskboard_maintenance_schedule", "maintenance-show", project, **project)
        self.assertEqual(maintenance["schedule"]["members"], [actor])
        self.assertTrue((await reviewer.call_tool("taskboard_maintenance_schedule", project)).is_error)

        action("save-goal", {**project, "kind": "standing", "summary": "Improve fixture readback"})
        standing = action("my-goals", **project)["standing_goal"]["ticket"]
        action("set-goal-review-time", {"id": standing, "time": "09:15"})
        goals = await compare("taskboard_goals", "my-goals", project, **project)
        self.assertEqual(goals["review_times"], {standing: "09:15"})
        opted = await owner.call_tool("taskboard_save_standing_goal", {
            **project, "summary": "Improve fixture readback", "keep_exploring": True,
            "stopping_criterion": "Two fictional receipts checked",
        })
        self.assertFalse(opted.is_error, opted.content)
        saved = await compare("taskboard_goals", "my-goals", project, **project)
        self.assertEqual(saved["standing_goal"]["stopping_criterion"], "Two fictional receipts checked")
        action("advance-task", {"id": standing, "transition": "start", "note": "Review the first fixture receipt."})
        continued = await owner.call_tool("taskboard_standing_pass", {
            "ticket": standing, "note": "One receipt matched.",
        })
        self.assertFalse(continued.is_error, continued.content)
        parked = await owner.call_tool("taskboard_standing_pass", {
            "ticket": standing, "note": "Both fictional receipts matched.", "criterion_met": True,
        })
        self.assertFalse(parked.is_error, parked.content)
        self.assertEqual(action("task", id=standing)["task"]["state"], "parked")

        action("set-wake-subscription", {**project, "event_type": "owned-ready", "enabled": "true"})
        subs = await compare("taskboard_wake_subscriptions", "my-wake-subscriptions", project, **project)
        self.assertTrue(subs["subscriptions"])

        entry = action("create-knowledge", {**project, "title": "Fixture index", "brief": "Collect readbacks."})["id"]
        action("knowledge-assign", {"id": entry, "dri": actor})
        action("knowledge-place", {"id": entry, "area": "Readback", "index": "true"})
        areas = await compare("taskboard_knowledge_areas", "knowledge-areas", project, **project)
        self.assertTrue(areas["areas"])
        sweep = await compare("taskboard_knowledge_sweep", "knowledge-sweep", {**project, "scope": "assigned", "limit": 20},
                              **project, scope="assigned")
        self.assertEqual([item["key"] for item in sweep["items"]], [entry])

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
            "taskboard_recurring_schedule", "taskboard_maintenance_schedule", "taskboard_goals", "taskboard_wake_subscriptions",
            "taskboard_knowledge_areas", "taskboard_knowledge_sweep", "taskboard_worker_checkpoint",
            "taskboard_project_access", "taskboard_set_project_access", "taskboard_agent_usage",
            "taskboard_delegate", "taskboard_manual_review",
            "taskboard_save_standing_goal", "taskboard_standing_pass",
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
            await value(owner, "taskboard_delegate", ticket=ticket, role="review",
                        worker=blake["id"], brief="Verify the fixture.")
        assigned = await value(reviewer, "taskboard_reviews", project="T", limit=1)
        self.assertEqual([row["task_key"] for row in assigned["items"]], ["T-1"])
        run = f"W-{assigned['items'][0]['id']}"
        checkpoint = await value(reviewer, "taskboard_worker_checkpoint", run=run)
        self.assertIsNone(checkpoint["checkpoint"])
        self.assertEqual(checkpoint, await value(owner, "taskboard_worker_checkpoint", run=run))
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
