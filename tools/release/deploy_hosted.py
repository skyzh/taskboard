"""Deploy new protected GitHub main revisions on the authorized Pi operator."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

from ci_watch import build_checked_commit, resolve_commit
from deploy_main import predecessor_for
from github_main import (
    GitHubRateLimited, read_github, repository_path, require_protected_tip, respect_rate_limit, verify_required_checks,
)
from local_queue import queue_lock, write_json
from operator_deploy import RETRY_ATTEMPT, deployment_layout, read_operator_config, run_config
from queue_provenance import GIT_COMMIT, SHA256
from release_compatibility import run_matrix, verify_policy_receipt
from release_plan import ReleaseRejected, file_hash
from stage_transfer import stage_transfer
from tasks_release_info import METADATA_NAME, release_info


MAIN_REF = "refs/heads/main"


def public_source(settings):
    repo = repository_path(settings).removeprefix("/repos/")
    return f"https://github.com/{repo}.git"


def git_main_tip(settings):
    result = subprocess.run(
        ["git", "ls-remote", "--exit-code", "--refs", public_source(settings), MAIN_REF],
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, capture_output=True, text=True, check=True, timeout=15,
    )
    fields = result.stdout.split()
    if len(fields) != 2 or fields[1] != MAIN_REF or GIT_COMMIT.fullmatch(fields[0]) is None:
        raise ReleaseRejected("GitHub did not return an exact SHA for main.")
    return fields[0]


def fetch_commit(repo, settings, commit):
    subprocess.run(
        ["git", "-C", repo, "fetch", "--no-tags", public_source(settings), MAIN_REF],
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, capture_output=True, check=True, timeout=90,
    )
    if resolve_commit(repo, "FETCH_HEAD") != commit:
        raise ReleaseRejected("GitHub main advanced before fetching the checked revision.")


def save_running_release(layout, evidence, outcome):
    build = evidence["build"]
    active = file_hash(layout.current / "SHA256SUMS")
    if (outcome["commit"] != build["commit"]
            or not outcome["active"] == outcome["candidate"] == build["candidate"] == active):
        raise ReleaseRejected("The saved operator result does not match the running release.")
    write_json(layout.releases / METADATA_NAME, {
        "manifest": outcome["active"],
        "commit": outcome["commit"],
        "built_at": build["built_at"],
        "deployed_at": outcome["at"],
    })


def deploy_once(config, *, tip=git_main_tip, read=read_github, fetch=fetch_commit, operate=run_config,
                retry_commit=None, attempt=None):
    if type(config.get("operator_uid")) is not int or config["operator_uid"] != os.getuid():
        raise ValueError("Run under the existing authorized Taskboard service account.")
    if (retry_commit is None) != (attempt is None) or (attempt is not None and (
        GIT_COMMIT.fullmatch(retry_commit) is None or RETRY_ATTEMPT.fullmatch(attempt) is None
    )):
        raise ValueError("A retry requires the full protected commit and a 32-character hexadecimal attempt ID.")
    settings = config["github"]
    layout = deployment_layout(config)
    state = Path(config["ci_state"])
    records = state / "hosted-deployments"
    records.mkdir(mode=0o700, parents=True, exist_ok=True)
    with queue_lock(records):
        commit = tip(settings)
        if retry_commit is not None and commit != retry_commit:
            raise ReleaseRejected("The rejected commit is no longer the protected main tip.")
        if attempt is None and release_info(layout.current)["commit"] == commit:
            return {"commit": commit, "status": "unchanged"}
        rate_limit = records / "github-rate-limit.json"
        try:
            return deploy_changed(config, layout, records, commit,
                                  read=respect_rate_limit(read, rate_limit), fetch=fetch, operate=operate,
                                  attempt=attempt)
        except GitHubRateLimited as error:
            return {"commit": commit, "status": "rate-limited", "retry_at": error.retry_at}


def record_staged_failure(records, commit, candidate, stage, error):
    if not (
        isinstance(commit, str) and GIT_COMMIT.fullmatch(commit)
        and isinstance(candidate, str) and SHA256.fullmatch(candidate)
    ):
        return
    invocation = os.environ.get("INVOCATION_ID", "")
    attempt = invocation if re.fullmatch(r"[0-9a-f]{32}", invocation) else uuid4().hex
    if isinstance(error, ReleaseRejected):
        reason = "rejected"
    elif isinstance(error, subprocess.SubprocessError):
        reason = "command_failed"
    elif isinstance(error, OSError):
        reason = "io_error"
    else:
        reason = "operation_failed"
    try:
        write_json(records / "last-staged-failure.json", {
            "schema_version": 1,
            "attempt": attempt,
            "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "commit": commit,
            "candidate": candidate,
            "status": "unconfirmed",
            "stage": stage,
            "reason_code": reason,
        })
    except OSError:
        print("Could not save the private staged deployment failure receipt.", file=sys.stderr)


def deploy_changed(config, layout, records, commit, *, read, fetch, operate, attempt=None):
    settings = config["github"]
    state = records.parent
    python = config.get("python", sys.executable)
    original_checked = records / f"{commit}.json"
    checked = original_checked if attempt is None else records / f"{commit}-retry-{attempt}.json"
    attempt_args = {"attempt": attempt} if attempt is not None else {}
    gate = None if checked.is_file() else verify_required_checks(settings, commit, read)
    target = operate(config, lambda _: None, checked_main=commit, read_state=True, **attempt_args)
    outcome = target["outcome"]
    if outcome is None:
        gate = gate or verify_required_checks(settings, commit, read)
        if attempt is None:
            fetch(Path(config["repo"]), settings, commit)
            build = build_checked_commit(config["repo"], state, commit, python)
        else:
            original = target["original"]
            if target["active"] != original["base"]:
                raise ReleaseRejected("The active base changed after the rejected release.", "stale_base")
            if not original_checked.is_file():
                raise ReleaseRejected("The rejected release has no saved checked build metadata.")
            build = json.loads(original_checked.read_text(encoding="utf-8"))["build"]
            if build["commit"] != commit or build["candidate"] != original["candidate"]:
                raise ReleaseRejected("The saved build does not match the rejected artifact.")
        candidate = build["candidate"]
        artifact = state / "artifacts" / candidate
        stage = "compatibility"
        try:
            base = target["active"]
            predecessor = predecessor_for(config, state, base)
            policy = json.loads(Path(config["compatibility_policy"]).read_text(encoding="utf-8"))
            matrix = run_matrix(artifact, candidate, predecessor, base, policy, python)
            verify_policy_receipt(matrix, candidate, base, policy, predecessor)
            evidence = {"github": gate, "build": build, "compatibility": matrix}

            def retry_preflight():
                require_protected_tip(settings, commit, read)
                verify_policy_receipt(matrix, candidate, base, policy, predecessor)

            stage = "transfer"
            with TemporaryDirectory(prefix=".package-", dir=records) as directory:
                archive = Path(directory) / "release.tar"
                stage_transfer(artifact, candidate, archive)
                stage = "protected_tip"
                require_protected_tip(settings, commit, read)
                stage = "checked_receipt"
                write_json(checked, evidence)
                with archive.open("rb") as source:
                    stage = "operator_handoff"
                    outcome = operate(config, lambda _: None, checked_main=commit, candidate=candidate,
                                      base=base, source=source, **attempt_args,
                                      **({"preflight": retry_preflight} if attempt is not None else {}))
        except GitHubRateLimited:
            raise
        except Exception as error:
            # Preserve the launcher exit while leaving a diagnostic for a staged attempt.
            if outcome is None:
                record_staged_failure(records, commit, candidate, stage, error)
            raise
    elif checked.is_file():
        evidence = json.loads(checked.read_text(encoding="utf-8"))
    else:
        raise ReleaseRejected("This commit has a saved release without hosted build metadata.")

    if outcome["status"] == "released":
        save_running_release(layout, evidence, outcome)
    return {**outcome, "evidence": evidence}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Private Pi operator configuration.")
    parser.add_argument("--retry-commit", help="Exact still-protected main commit with a saved rejection.")
    parser.add_argument("--attempt-id", help="Explicit attempt ID for this checked retry.")
    args = parser.parse_args()
    try:
        outcome = deploy_once(read_operator_config(args.config), retry_commit=args.retry_commit, attempt=args.attempt_id)
    except (OSError, KeyError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Deploy protected main: {error}\n")
    print(json.dumps(outcome))
    return int(outcome["status"] not in {"released", "unchanged", "rate-limited"})


if __name__ == "__main__":
    raise SystemExit(main())
