#!/usr/bin/env python3
"""Run selected Tasks Board fixtures from a pinned, disposable artifact copy."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory


PYTHON_TESTS = {
    "priority": (
        "test_tasks.TasksTest.test_priority_reads_legacy_db_and_keeps_ready_age_and_state",
        "test_tasks_service.TasksServiceTest.test_shared_priority_order_and_cursor_survive_a_handoff",
        "test_tasks_service.TasksServiceTest.test_task_search_filters_authorized_current_rows_before_pagination",
    ),
    "restart": (
        "test_tasks_service.TasksServiceTest.test_durable_session_resumes_human_and_shared_cli_after_restart",
        "test_raft_auth.RaftAuthTest.test_durable_session_expires_and_is_bound_to_original_mount",
        "test_raft_auth.RaftAuthTest.test_durable_storage_rejects_public_directory_and_file",
    ),
}
REVISION_TEST = "web/src/data.test.mjs"
TLS_PROBE = Path("session-restart-proof.vDgCoTGD/restart_probe.py")
TLS_EXPECTED = {
    "restarted_process": True, "human_http_session": True,
    "agent_shared_cli_session": True, "fresh_login_after_restart": False,
}
CHECKSUM_LINE = re.compile(r"([0-9a-f]{64}) [ *](.+)")
PYTHON_HARNESS = """\
from contextlib import redirect_stdout
from pathlib import Path
import json, sys, unittest

if Path("tools/checkout.py").is_file():
    from tools.checkout import add_packages, add_tests

    add_packages("client", "cli", "server")
    add_tests()

with redirect_stdout(sys.stderr):
    tests = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])
    result = unittest.TextTestRunner(verbosity=2).run(tests)
print(json.dumps({"tests": result.testsRun, "failures": len(result.failures),
                  "errors": len(result.errors), "skipped": len(result.skipped),
                  "expected_failures": len(result.expectedFailures)}))
sys.exit(not (result.wasSuccessful() and result.testsRun == len(sys.argv) - 1
              and not result.skipped and not result.expectedFailures))
"""


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def stage_artifact(source, stage, expected_hash, verified):
    manifest = (source / "SHA256SUMS").read_bytes()
    actual_hash = hashlib.sha256(manifest).hexdigest()
    verified["observed_sha256"] = actual_hash
    if actual_hash != expected_hash:
        raise ValueError("SHA256SUMS does not match the expected manifest SHA-256")
    files = set()
    for line in manifest.decode("utf-8").splitlines():
        entry = CHECKSUM_LINE.fullmatch(line)
        if not entry:
            raise ValueError("Malformed line in SHA256SUMS")
        expected, name = entry.groups()
        relative = PurePosixPath(name)
        if (
            relative.is_absolute() or ".." in relative.parts
            or relative.as_posix() != name or relative in files
        ):
            raise ValueError(f"Invalid or duplicate manifest path: {name}")
        original = source / relative
        if not original.is_file() or original.is_symlink() or not original.resolve().is_relative_to(source):
            raise ValueError(f"Manifest file is missing or not a regular artifact file: {name}")
        destination = stage / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(original, destination)
        if checksum(destination) != expected:
            raise ValueError(f"Checksum mismatch: {name}")
        files.add(relative)
    if not files:
        raise ValueError("SHA256SUMS has no artifact files")
    verified["files"] = len(files)
    verified["status"] = "pass"


def executable(spec):
    binary = shutil.which(spec)
    if binary is None:
        raise ValueError(f"Runtime is not executable: {spec}")
    # Keep the venv entry point so Python finds the matching site packages.
    return os.path.abspath(binary)


def stage_node_dependencies(stage, source):
    modules = source.resolve()
    lock = modules.parent / "package-lock.json"
    if not modules.is_dir() or not lock.is_file():
        raise ValueError("--node-modules must point to an installed tree next to package-lock.json")
    if checksum(lock) != checksum(stage / "web/package-lock.json"):
        raise ValueError("The installed Node dependency lock differs from the artifact")
    (stage / "web/node_modules").symlink_to(modules, target_is_directory=True)


def fixture_env(scratch, *runtimes):
    paths = [str(Path(runtime).parent) for runtime in runtimes]
    return {
        "PATH": os.pathsep.join((*paths, os.defpath)),
        "TMPDIR": str(scratch),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
    }


def run_suite(suite, stage, scratch, args):
    check = {"id": suite, "status": "fail"}
    try:
        if suite in PYTHON_TESTS:
            runtime = executable(args.python)
            command = [runtime, "-B", "-W", "error::ResourceWarning", "-c", PYTHON_HARNESS, *PYTHON_TESTS[suite]]
            check["tests"] = list(PYTHON_TESTS[suite])
        else:
            runtime = executable(args.node)
            source = args.node_modules or args.artifact / "web/node_modules"
            stage_node_dependencies(stage, source)
            command = [runtime, "--test", "--test-reporter=tap", REVISION_TEST]
        check["command"] = command
        run = subprocess.run(
            command, cwd=stage, env=fixture_env(scratch, runtime), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=90,
        )
        check["exit_code"] = run.returncode
        if suite in PYTHON_TESTS:
            counts = json.loads(run.stdout or "{}")
            check["counts"] = counts
            complete = counts.get("tests") == len(PYTHON_TESTS[suite])
        else:
            counts = {
                name: int(value) for name, value in re.findall(
                    r"^# (tests|pass|fail|cancelled|skipped|todo) (\d+)$", run.stdout, re.MULTILINE,
                )
            }
            check["counts"] = counts
            complete = (
                counts.get("tests", 0) > 0
                and counts.get("pass") == counts["tests"]
                and all(counts.get(name) == 0 for name in ("fail", "cancelled", "skipped", "todo"))
            )
        if run.returncode == 0 and complete:
            check["status"] = "pass"
        else:
            check["error"] = "Fixture failed or did not complete all selected tests"
            check["diagnostic"] = (run.stderr or run.stdout).strip()[-1500:]
    except subprocess.TimeoutExpired:
        check["error"] = "Fixture timed out after 90 seconds"
    except (OSError, ValueError) as error:
        check["error"] = str(error)
    return check


def copy_pinned_script(source, destination, expected):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(source, destination)
    actual = checksum(destination)
    if actual != expected:
        raise ValueError(f"Checksum mismatch for TLS check dependency: {source.name}")
    return actual


def run_tls_restart(stage, scratch, args):
    check = {"id": "tls-restart", "status": "fail"}
    try:
        if not (args.tls_launcher and args.tls_launcher_sha256 and args.tls_probe_sha256):
            raise ValueError("Use --tls-launcher, --tls-launcher-sha256, and --tls-probe-sha256")
        source = args.tls_launcher.resolve()
        scripts = stage.parent / "scripts"
        launcher = scripts / "launcher" / "run-restart-check.py"
        check["launcher_sha256"] = copy_pinned_script(source, launcher, args.tls_launcher_sha256)
        check["probe_sha256"] = copy_pinned_script(
            source.parent.parent / TLS_PROBE, scripts / TLS_PROBE, args.tls_probe_sha256,
        )
        runtime = executable(args.python)
        openssl = executable("openssl")
        check["command"] = [runtime, "-B", str(source), str(args.artifact.resolve())]
        with subprocess.Popen(
            [runtime, "-B", str(launcher), str(stage)], cwd=stage,
            env=fixture_env(scratch, runtime, openssl), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", start_new_session=True,
        ) as process:
            try:
                output, errors = process.communicate(timeout=60)
            finally:
                # The launcher can time out with a fixture server still alive.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        check["exit_code"] = process.returncode
        if process.returncode != 0:
            raise ValueError("Disposable TLS launcher failed: " + (errors or output).strip()[-1000:])
        proof = json.loads(output)
        if not isinstance(proof, dict) or any(proof.get(key) is not value for key, value in TLS_EXPECTED.items()):
            raise ValueError("Disposable TLS launcher did not report all required restart results")
        check["evidence"] = {key: proof[key] for key in TLS_EXPECTED}
        check["status"] = "pass"
    except subprocess.TimeoutExpired:
        check["error"] = "Disposable TLS restart check timed out after 60 seconds"
    except (OSError, ValueError) as error:
        check["error"] = str(error)
    return check


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--suite", action="append", choices=(*PYTHON_TESTS, "revision", "tls-restart"), required=True)
    parser.add_argument("--python", default=sys.executable, help="Interpreter with Tasks Board test dependencies")
    parser.add_argument("--node", default="node")
    parser.add_argument("--node-modules", type=Path, help="Installed dependencies for the revision fixture")
    parser.add_argument("--tls-launcher", type=Path, help="Trusted disposable restart launcher")
    parser.add_argument("--tls-launcher-sha256", help="Expected digest of the trusted restart launcher")
    parser.add_argument("--tls-probe-sha256", help="Expected digest of its sibling restart probe")
    args = parser.parse_args()
    source = args.artifact.resolve()
    verified = {"id": "artifact-manifest", "status": "fail"}
    receipt = {
        "schema_version": 1, "status": "fail", "manifest_sha256": args.manifest_sha256,
        "invocation": [sys.executable, *sys.argv],
        "checks": [verified],
    }
    try:
        with TemporaryDirectory(prefix="tasks-board-fixtures-") as directory:
            work = Path(directory).resolve()
            stage = work / "release"
            scratch = work / "scratch"
            scratch.mkdir()
            stage_artifact(source, stage, args.manifest_sha256, verified)
            for suite in dict.fromkeys(args.suite):
                if suite == "tls-restart":
                    check = run_tls_restart(stage, scratch, args)
                else:
                    check = run_suite(suite, stage, scratch, args)
                receipt["checks"].append(check)
    except (OSError, ValueError) as error:
        if verified["status"] == "pass":
            receipt["checks"].append({"id": "fixture-workspace", "status": "fail", "error": str(error)})
        else:
            verified["error"] = str(error)
    success = all(check["status"] == "pass" for check in receipt["checks"])
    receipt["status"] = "pass" if success else "fail"
    print(json.dumps(receipt))
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
