"""Verify clean client, CLI, MCP and server wheel installs with synthetic inputs."""

from contextlib import closing
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from urllib.error import URLError
from urllib.request import urlopen

from build_packages import build
from checkout import ROOT


def run(args, *, cwd, environment, **kwargs):
    return subprocess.run(args, cwd=cwd, env=environment, check=True, timeout=90, **kwargs)


def wheel(directory, component):
    return next(directory.glob(f"taskboard_{component}-*.whl"))


def check(directory):
    wheels = directory / "wheels"
    build(wheels)
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("TASKS_", "TASKBOARD_"))}
    shim = directory / "bin"
    shim.mkdir()
    raft = shim / "raft"
    raft.write_text(f"""#!{sys.executable}
import json
import sys
assert sys.argv[1:5] == ["integration", "invoke", "--service", "tasks-board"]
assert sys.argv[6] == "context"
actor = {{"id": "raft:agent:fixture", "name": "Avery", "username": "avery", "kind": "agent"}}
print(json.dumps({{"ok": True, "data": {{"result": {{"actor": actor, "board": "https://example.test/tasks/"}}}}}}))
""")
    raft.chmod(0o700)
    fixture_env = {**environment, "PATH": os.pathsep.join((str(shim), environment["PATH"]))}

    def install(name, components, *, locked_server=False, locked_mcp=False):
        venv = directory / name
        if locked_server:
            run(["uv", "sync", "--locked", "--no-dev", "--python", sys.executable],
                cwd=ROOT, environment={**environment, "UV_PROJECT_ENVIRONMENT": str(venv)})
        else:
            run(["uv", "venv", "--python", sys.executable, venv], cwd=directory, environment=environment)
        executable = venv / "bin" / "python"
        if locked_mcp:
            requirements = directory / "mcp-requirements.txt"
            run(["uv", "export", "--locked", "--script", ROOT / "tasks_mcp.py", "--output-file", requirements],
                cwd=ROOT, environment=environment)
            run(["uv", "pip", "sync", "--require-hashes", "--python", executable, requirements],
                cwd=directory, environment=environment)
        run(["uv", "pip", "install", "--python", executable, "--no-deps", "--no-index",
             *(wheel(wheels, component) for component in components)], cwd=directory, environment=environment)
        if locked_mcp:
            run(["uv", "pip", "check", "--python", executable], cwd=directory, environment=environment)
        return venv / "bin"

    client = install("client", ("client",))
    run([client / "python", "-I", "-c", "from taskboard_client import SharedBoard; assert SharedBoard('sample').service == 'sample'"],
        cwd=directory, environment=fixture_env)
    cli = install("cli", ("client", "cli"))
    result = run([cli / "tasks", "--shared", "context"], cwd=directory, environment=fixture_env,
                 capture_output=True, text=True)
    assert "Avery" in result.stdout and "https://example.test/tasks/" in result.stdout
    run([cli / "python", "-I", "-c", "import importlib.util; assert importlib.util.find_spec('tasks_core') is None"],
        cwd=directory, environment=environment)
    mcp = install("mcp", ("client", "mcp"), locked_mcp=True)
    run([mcp / "taskboard-mcp"], cwd=directory, environment=fixture_env, input="", text=True, capture_output=True)
    run([mcp / "python", "-I", ROOT / "packages/mcp/tests/test_installed_mcp.py"],
        cwd=directory, environment=fixture_env)
    run([mcp / "python", "-I", "-c", "import importlib.util; assert importlib.util.find_spec('taskboard_cli') is None"],
        cwd=directory, environment=environment)

    server = install("server", ("client", "server"), locked_server=True)
    run([mcp / "python", "-I", ROOT / "tools/test_mcp_stdio.py"], cwd=directory,
        environment={**environment, "TASKBOARD_MCP_FIXTURE_PYTHON": str(server / "python")})
    database = directory / "preview.sqlite3"
    run([sys.executable, ROOT / "tasks", "--db", database, "add", "Verify the installed board",
         "--dri", "Avery", "--step", "Read installed assets."], cwd=directory, environment=environment)
    with closing(socket.socket()) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with subprocess.Popen(
        [server / "taskboard-serve", "--db", database, "--local-readonly", "--port", str(port)],
        cwd=directory, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    ) as process:
        try:
            origin = f"http://127.0.0.1:{port}/"
            deadline = monotonic() + 12
            while monotonic() < deadline and process.poll() is None:
                try:
                    with urlopen(origin, timeout=1) as response:
                        markup = response.read().decode()
                    break
                except URLError:
                    sleep(0.05)
            else:
                raise RuntimeError("The installed server did not start.")
            assert "Taskboard" in markup
            for asset in re.findall(r'(?:href|src)="\./(assets/[^"]+)"', markup):
                with urlopen(origin + asset, timeout=2) as response:
                    assert response.read(1)
            with urlopen(origin + "assets/licenses/lucide-react.LICENSE", timeout=2) as response:
                assert response.read(1)
            with urlopen(origin + "api/v1/tasks/1", timeout=2) as response:
                assert json.load(response)["task"]["note"] == "Read installed assets."
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


if __name__ == "__main__":
    with TemporaryDirectory(prefix="taskboard-package-proof-") as temporary:
        check(Path(temporary))
    print("Four clean installs passed. The installed server served React and the preserved SQLite ticket.")
