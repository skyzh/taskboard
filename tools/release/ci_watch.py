"""Check immutable Taskboard commits and stage reproducible release artifacts."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import time

from release_plan import verify_manifest

def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", repo, *args], stderr=subprocess.PIPE, timeout=15,
    )


def resolve_commit(repo, ref):
    return git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()


def export_commit(repo, commit, root):
    """Extract tracked ordinary files only. The worktree and index stay untouched."""
    files = []
    with tarfile.open(fileobj=BytesIO(git(repo, "archive", "--format=tar", commit))) as archive:
        for member in archive:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Unexpected path in the Git snapshot.")
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError("CI inputs must use ordinary tracked files.")
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
            files.append(path)
    return files


def write_json(path, value):
    with TemporaryDirectory(prefix=".receipt-", dir=path.parent) as temporary:
        staging = Path(temporary) / "receipt"
        staging.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(staging, path)


def affected_checks(repo, commit, prior):
    """Share unchanged backend checks only from a previous passing ancestor."""
    if not prior or prior["status"] != "pass":
        return {"python", "web"}
    base = prior["commit"]
    ancestry = subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", base, commit], timeout=15)
    if ancestry.returncode:
        return {"python", "web"}
    paths = [os.fsdecode(name) for name in git(repo, "diff", "--name-only", "-z", base, commit).split(b"\0") if name]
    if paths and all(name.startswith("web/") for name in paths):
        return {"web"}
    return {"python", "web"}


def run_check(cwd, command, log, python):
    started = time.monotonic()
    environment = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
    environment["PATH"] = str(Path(python).absolute().parent) + os.pathsep + environment.get("PATH", os.defpath)
    with log.open("ab") as output:
        try:
            result = subprocess.run(command, cwd=cwd, env=environment,
                                    stdout=output, stderr=subprocess.STDOUT, timeout=240)
        except subprocess.TimeoutExpired:
            return {"status": "fail", "reason": "timeout", "elapsed_ms": round((time.monotonic() - started) * 1000)}
    return {"status": "pass" if result.returncode == 0 else "fail",
            "elapsed_ms": round((time.monotonic() - started) * 1000)}


def build_web(root, log, python, npm, *, offline=True):
    web = root / "web"
    install = [npm, "ci", "--no-audit", "--no-fund"]
    if offline:
        install.append("--offline")
    checks = {"install": run_check(web, install, log, python)}
    if checks["install"]["status"] == "pass":
        checks["web"] = run_check(web, [npm, "run", "build"], log, python)
    return checks


def publish_artifact(root, files, artifacts):
    # Use only committed sources and the fresh built assets, without caches.
    files = {file for file in files if not file.is_relative_to("web/dist")}
    files.update(file.relative_to(root) for file in (root / "web/dist").rglob("*") if file.is_file())
    if not (root / "web/dist/index.html").is_file():
        raise ValueError("The checked build did not produce its board entry point.")
    raw = "".join(f"{sha256((root / file).read_bytes()).hexdigest()}  {file}\n" for file in sorted(files)).encode()
    digest = sha256(raw).hexdigest()
    artifacts.mkdir(parents=True, exist_ok=True)
    release = artifacts / digest
    if not release.exists():
        with TemporaryDirectory(prefix=".artifact-", dir=artifacts) as temporary:
            stage = Path(temporary) / "candidate"
            stage.mkdir()
            for file in files:
                output = stage / file
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / file, output)
            (stage / "SHA256SUMS").write_bytes(raw)
            stage.rename(release)
    verify_manifest(release, digest)
    return digest


@contextmanager
def ci_lock(state):
    with (state / ".ci-lock").open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Another Taskboard CI runner is using this state.") from error
        yield


def check_commit(repo, state, ref, python, npm="npm"):
    repo, state = Path(repo).resolve(), Path(state).resolve()
    if state.is_relative_to(repo):
        raise ValueError("Use a private CI state directory outside the source repository.")
    state.mkdir(parents=True, exist_ok=True)
    with ci_lock(state):
        return check_snapshot(repo, state, resolve_commit(repo, ref), python, npm)


def build_checked_commit(repo, state, commit, python, npm="npm"):
    """Package an exact revision whose required tests passed on GitHub."""
    repo, state = Path(repo).resolve(), Path(state).resolve()
    if state.is_relative_to(repo):
        raise ValueError("Use a private CI state directory outside the source repository.")
    builds = state / "hosted-builds"
    builds.mkdir(parents=True, exist_ok=True)
    with ci_lock(state), TemporaryDirectory(prefix="taskboard-build-", dir=state) as temporary:
        root = Path(temporary)
        log = builds / f"{commit}.log"
        files = export_commit(repo, commit, root)
        checks = build_web(root, log, python, npm, offline=False)
        if any(result["status"] != "pass" for result in checks.values()):
            raise ValueError(f"The locked web build failed. See {log}.")
        candidate = publish_artifact(root, files, state / "artifacts")
        return {"commit": commit, "candidate": candidate, "built_at": utcnow(), "checks": checks}


def assemble_and_check(repo, state, ref, siblings, python, npm="npm"):
    """Try reviewed candidate branches together in a separate Git index."""
    repo, state = Path(repo).resolve(), Path(state).resolve()
    if state.is_relative_to(repo):
        raise ValueError("Use a private CI state directory outside the source repository.")
    state.mkdir(parents=True, exist_ok=True)
    with ci_lock(state), TemporaryDirectory(prefix="taskboard-ci-integrate-", dir=state) as directory:
        scratch = Path(directory) / "repo"
        base = resolve_commit(repo, ref)
        commits = [resolve_commit(repo, sibling) for sibling in siblings]
        subprocess.run(
            ["git", "clone", "--quiet", "--no-checkout", "--local", repo, scratch],
            check=True, capture_output=True, timeout=15,
        )
        git(scratch, "checkout", "--quiet", "--detach", base)
        for commit in dict.fromkeys(commits):
            common = git(scratch, "merge-base", base, commit).decode().strip()
            patch = git(scratch, "diff", "--binary", "--full-index", common, commit)
            if patch:
                applied = subprocess.run(
                    ["git", "-C", scratch, "apply", "--3way", "--index", "-"],
                    input=patch, capture_output=True, timeout=15,
                )
                if applied.returncode:
                    return {"status": "conflict", "base": base, "inputs": commits,
                            "conflicting_commit": commit}
        git(scratch, "-c", "user.name=Taskboard CI", "-c", "user.email=ci@example.test",
            "commit", "--quiet", "--allow-empty", "-m", "Check the combined release snapshot")
        return check_snapshot(scratch, state, resolve_commit(scratch, "HEAD"), python, npm,
                              integration={"base": base, "inputs": commits})


def check_snapshot(repo, state, commit, python, npm, integration=None):
    receipts = state / "commits"
    receipts.mkdir(exist_ok=True)
    receipt_path = receipts / f"{commit}.json"
    if receipt_path.exists():
        previous = json.loads(receipt_path.read_text())
        if previous["status"] == "pass":
            verify_manifest(state / "artifacts" / previous["candidate"], previous["candidate"])
        return previous
    pointer = state / "last-passing.json"
    prior = json.loads(pointer.read_text()) if pointer.exists() else None
    suites = affected_checks(repo, commit, prior)
    artifacts = state / "artifacts"
    log = receipts / f"{commit}.log"
    receipt = {"commit": commit, "started_at": utcnow(), "status": "fail", "checks": {}, **(integration or {})}
    with TemporaryDirectory(prefix="taskboard-ci-", dir=state) as temporary:
        root = Path(temporary)
        files = export_commit(repo, commit, root)
        web = root / "web"
        receipt["checks"].update(build_web(root, log, python, npm))
        if receipt["checks"].get("web", {}).get("status") == "pass":
            # Mounted Python fixtures need the matching built frontend.
            if "python" in suites:
                receipt["checks"]["python"] = run_check(root, [
                    python, "-B", "-W", "error::ResourceWarning", "-m", "unittest", "discover", "-q",
                ], log, python)
                if (root / "tools" / "release" / "test_ci_watch.py").is_file():
                    receipt["checks"]["release"] = run_check(root, [
                        python, "-B", "-W", "error::ResourceWarning", "-m", "unittest",
                        "discover", "-s", "tools/release", "-q",
                    ], log, python)
            else:
                receipt["checks"]["python"] = {"status": "inherited", "commit": prior["commit"]}
            tests = sorted(str(path.relative_to(web)) for path in (web / "src").rglob("*.test.mjs"))
            if not tests:
                raise ValueError("No Node tests were present in the snapshot.")
            receipt["checks"]["node"] = run_check(web, ["node", "--test", *tests], log, python)
        if all(check["status"] != "fail" for check in receipt["checks"].values()):
            receipt["candidate"] = publish_artifact(root, files, artifacts)
            receipt["status"] = "pass"
    receipt["finished_at"] = utcnow()
    write_json(receipt_path, receipt)
    if receipt["status"] == "pass" and integration is None:
        write_json(pointer, receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--ref", default="refs/heads/main")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--sibling", action="append", default=[], help="Candidate ref to combine with the base in a scratch index.")
    parser.add_argument("--interval", type=float, help="Watch the ref at this interval in seconds.")
    options = parser.parse_args()
    if options.interval is not None and options.interval < 1:
        parser.error("Use an interval of at least one second.")
    if options.sibling and options.interval is not None:
        parser.error("Run a batch check once. Use watch mode for a single branch.")
    if options.sibling:
        result = assemble_and_check(options.repo, options.state, options.ref, options.sibling, options.python)
        print(json.dumps(result))
        return 0 if result["status"] == "pass" else 1
    previous = None
    while True:
        try:
            receipt = check_commit(options.repo, options.state, options.ref, options.python)
            if receipt["commit"] != previous:
                print(json.dumps(receipt), flush=True)
            previous = receipt["commit"]
            if options.interval is None:
                return 0 if receipt["status"] == "pass" else 1
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"Taskboard CI: {error}", file=sys.stderr, flush=True)
            if options.interval is None:
                return 1
        time.sleep(options.interval)


if __name__ == "__main__":
    raise SystemExit(main())
