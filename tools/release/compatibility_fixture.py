"""Exercise pinned Taskboard clients and mounted APIs with synthetic identity."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import selectors
from shlex import quote
import ssl
import subprocess
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


MOUNT = "/tasks"
SERVICE = "fixture-tasks"
PORTAL = {"X-Portal-Upstream-Token": "synthetic-portal-fixture"}
MANIFEST = MOUNT + "/.well-known/raft-agent-manifest.json"
LEGACY_TASK = "Fixture normal priority"
COURSE_TASK = "Fixture high priority in COURSE"
SCRIPT = Path(__file__).resolve()


class CompatibilityFailure(ValueError):
    pass


def expect(condition, message):
    if not condition:
        raise CompatibilityFailure(message)


def request(origin, path, *, data=None, cookie=None, query=None):
    expect(path.startswith(MOUNT + "/") and not path.startswith("//"), "The fixture action must use the mounted service.")
    headers = dict(PORTAL)
    if cookie:
        headers["Cookie"] = cookie
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    if query:
        path += "?" + urlencode(query)
    return urlopen(Request(origin + path, body, headers), timeout=10,
                   context=ssl._create_unverified_context())


def read_json(origin, path, **options):
    with request(origin, path, **options) as response:
        return json.load(response)


def actions(manifest):
    expect(manifest["service"] == SERVICE, "The mounted fixture returned the wrong app manifest.")
    return {entry["name"]: entry for entry in manifest["actions"]}


def io_action(origin, name, *, cookie, data=None, query=None):
    return read_json(origin, MOUNT + "/api/v1/raft/" + name, data=data, query=query, cookie=cookie)


def verify_contract(predecessor, candidate):
    for name, prior in predecessor.items():
        expect(name in candidate, f"Shared action {name} is missing from the candidate.")
        current = candidate[name]
        expect(current["endpoint"] == prior["endpoint"], f"Shared action {name} changed method or route.")
        old_params, new_params = prior["parameters"], current["parameters"]
        for field, old in old_params.items():
            expect(field in new_params and new_params[field]["type"] == old["type"],
                   f"Shared action {name} removed or changed parameter {field}.")
        for field, new in new_params.items():
            expect(not new["required"] or old_params.get(field, {}).get("required"),
                   f"Shared action {name} added required parameter {field}.")


def run_raft(arguments):
    origin = os.environ["TASKBOARD_COMPAT_ORIGIN"]
    cookie = os.environ["TASKBOARD_COMPAT_COOKIE"]
    expect(arguments[:4] == ["integration", "invoke", "--service", SERVICE],
           "The CLI invoked an unexpected fixture command.")
    name = arguments[arguments.index("--action") + 1]
    manifest = actions(read_json(origin, MANIFEST))
    if name not in manifest:
        if trace := os.environ.get("TASKBOARD_COMPAT_MISSING_ACTION"):
            Path(trace).write_text(name)
        print(json.dumps({"ok": False, "error": {"code": "INTEGRATION_ACTION_NOT_FOUND"}}))
        return 1
    endpoint = manifest[name]["endpoint"]
    fields = dict(arguments[index + 1].split("=", 1) for index, value in enumerate(arguments) if value == "--param")
    payload = json.load(sys.stdin) if "--data-file" in arguments else None
    expect(endpoint["method"] == ("POST" if payload is not None else "GET"),
           f"The client uses the wrong HTTP method for {name}.")
    try:
        result = read_json(origin, endpoint["path"], data=payload, query=fields, cookie=cookie)
    except HTTPError as error:
        with error:
            print(json.dumps({"ok": False, "error": {
                "code": "INTEGRATION_INVOKE_FAILED",
                "message": f"service action failed (HTTP {error.code})",
                "details": {"response_body": json.load(error)},
            }}))
        return 1
    print(json.dumps({"ok": True, "data": {"result": result}}))
    return 0


def serve_fixture(source, database, certificate, key, browser):
    sys.path.insert(0, str(source))
    if (Path(source) / "tools/checkout.py").is_file():
        from tools.checkout import add_packages, add_tests

        add_packages("client", "cli", "server")
        add_tests()
    from test_tasks_service import FixtureRaft, SERVER
    from tasks_service import create_app
    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", 0, lambda env, start: [],
                         ssl_context=(certificate, key))
    origin = f"https://127.0.0.1:{server.server_port}"
    server.app = create_app(
        database, public_url=origin + MOUNT, portal_reader=True,
        portal_upstream_token=PORTAL["X-Portal-Upstream-Token"],
        raft_config={
            "api_origin": "https://fixture-raft.invalid", "server_id": SERVER,
            "client_id": SERVICE, "client_secret": "synthetic-fixture-only", "transport": FixtureRaft(),
        },
        **({"dist": Path(browser) / "web" / "dist"} if browser else {}),
    )
    print(origin, flush=True)
    server.serve_forever()


@contextmanager
def fixture_server(python, source, directory, certificate, key, *, browser=None):
    directory.mkdir()
    database = directory / "fixture.sqlite3"
    command = [python, "-B", SCRIPT, "serve", source, database, certificate, key]
    if browser:
        command.append(browser)
    with (directory / "service.log").open("w") as log:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log, text=True) as process:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    expect(selector.select(15), "The mounted fixture server did not start.")
                    origin = process.stdout.readline().strip()
                expect(origin.startswith("https://127.0.0.1:"), "The fixture server did not return a loopback address.")
                with request(origin, MOUNT + "/auth/callback", query={"code": "Avery"}) as response:
                    cookie = response.headers["Set-Cookie"].split(";", 1)[0]
                env = {
                    "PATH": os.pathsep.join((str(directory), os.environ.get("PATH", os.defpath))),
                    "TASKS_RAFT_SERVICE": SERVICE,
                    "TASKBOARD_COMPAT_ORIGIN": origin, "TASKBOARD_COMPAT_COOKIE": cookie,
                }
                shim = directory / "raft"
                shim.write_text(f"#!/bin/sh\nexec {quote(str(python))} -B {quote(str(SCRIPT))} raft \"$@\"\n")
                shim.chmod(0o700)
                yield origin, cookie, database, env
            finally:
                process.terminate()
                process.wait(timeout=10)


def seed(python, source, origin, cookie, database):
    actor = io_action(origin, "context", cookie=cookie)["actor"]
    expect(actor["kind"] == "agent" and actor["name"] == "Avery", "The fixture did not sign in as Avery.")
    subprocess.run([python, "-B", source / "tasks", "--offline", "--db", database,
                    "register-project", "COURSE", "Synthetic project", "--dri", actor["id"]],
                   check=True, capture_output=True, text=True, timeout=15)
    for ticket, title, project, priority in (
        ("T-1", "Fixture low priority", "T", "low"),
        ("T-2", LEGACY_TASK, "T", "normal"),
        ("COURSE-1", COURSE_TASK, "COURSE", "high"),
    ):
        created = io_action(origin, "create-task", cookie=cookie,
                            data={"title": title, "step": "Review the fixture.", "project": project})
        expect("receipt" in created, "The fixture task did not return a creation receipt.")
        io_action(origin, "set-priority", cookie=cookie, data={"id": ticket, "priority": priority})
    return actor["id"]


def verify_queue(origin, cookie):
    params = {"state": "ready", "order": "priority", "limit": 1}
    first = io_action(origin, "my-queue", cookie=cookie, query=params)
    expect(first["next_cursor"] and [row["key"] for row in first["items"]] == ["T-2"], "Default API order changed.")
    second = io_action(origin, "my-queue", cookie=cookie, query={**params, "before": first["next_cursor"]})
    expect([row["key"] for row in second["items"]] == ["T-1"] and second["next_cursor"] is None,
           "Default API cursor or T scope changed.")


def cli(python, root, env, *arguments):
    return subprocess.run([python, "-B", root / "tasks", "--shared", *arguments],
                          cwd=root, env=env, capture_output=True, text=True, timeout=20)


def cli_success(python, root, env, *arguments):
    result = cli(python, root, env, *arguments)
    expect(result.returncode == 0, f"The pinned client failed {' '.join(arguments[:2])}: {result.stderr.strip()}")
    return result.stdout


def verify_cli(python, client, env, origin, cookie, actor, *, legacy=False):
    def run(*argv):
        return cli_success(python, client, env, *argv)

    expect("Avery" in run("context"), "The pinned client lost its shared identity.")
    listed = run("list", "--status", "ready")
    picked = run("next")
    missing = COURSE_TASK not in listed and COURSE_TASK not in picked
    expect(missing if legacy else COURSE_TASK in listed and COURSE_TASK in picked,
           "The pinned client's native queue default changed.")
    scoped = listed if legacy else run("list", "--status", "ready", "--project", "T")
    expect(LEGACY_TASK in scoped and "Fixture low priority" in scoped and COURSE_TASK not in scoped,
           "The pinned client lost its T-scoped queue.")
    expect(scoped.index(LEGACY_TASK) < scoped.index("Fixture low priority"), "The pinned CLI priority order changed.")
    expect(LEGACY_TASK in run("show", "T-2"), "The pinned client cannot read the expected task.")
    for command, line, extra in (
        ("start", "Compatibility fixture started.", ()),
        ("wait", "Compatibility fixture wait.", ("--on", "Fixture dependency")),
        ("resume", "Compatibility fixture resumed.", ()),
    ):
        expect("Committed" in run(command, "T-2", line, *extra), f"The pinned client lost the {command} receipt.")
    detail = io_action(origin, "task", cookie=cookie, query={"id": "T-2"})["task"]
    events = io_action(origin, "task-activity", cookie=cookie, query={"id": "T-2", "limit": 3})["items"]
    expect(detail["state"] == "in_progress" and detail["dri"] == actor, "The ledger did not retain the resumed task.")
    expect([(event["action"], event["actor"]) for event in events] == [(verb, actor) for verb in ("resume", "wait", "start")],
           "The ledger did not retain the exact attributed workflow.")


if __name__ == "__main__":
    mode, *arguments = sys.argv[1:]
    if mode == "raft":
        raise SystemExit(run_raft(arguments))
    serve_fixture(*arguments[:4], arguments[4] if len(arguments) == 5 else None)
