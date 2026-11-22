"""Deploy new protected GitHub main revisions on the authorized Pi operator."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from ci_watch import build_checked_commit, resolve_commit
from deploy_main import predecessor_for
from github_main import read_github, repository_path, require_protected_tip, verify_required_checks
from local_queue import queue_lock, write_json
from operator_deploy import deployment_layout, read_operator_config, run_config
from queue_provenance import GIT_COMMIT
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


def deploy_once(config, *, tip=git_main_tip, read=read_github, fetch=fetch_commit, operate=run_config):
    if type(config.get("operator_uid")) is not int or config["operator_uid"] != os.getuid():
        raise ValueError("Run under the existing authorized Taskboard service account.")
    settings = config["github"]
    layout = deployment_layout(config)
    state = Path(config["ci_state"])
    records = state / "hosted-deployments"
    records.mkdir(mode=0o700, parents=True, exist_ok=True)
    python = config.get("python", sys.executable)
    with queue_lock(records):
        commit = tip(settings)
        if release_info(layout.current)["commit"] == commit:
            return {"commit": commit, "status": "unchanged"}
        checked = records / f"{commit}.json"
        gate = None if checked.is_file() else verify_required_checks(settings, commit, read)
        target = operate(config, lambda _: None, checked_main=commit, read_state=True)
        outcome = target["outcome"]
        if outcome is None:
            gate = gate or verify_required_checks(settings, commit, read)
            fetch(Path(config["repo"]), settings, commit)
            build = build_checked_commit(config["repo"], state, commit, python)
            candidate = build["candidate"]
            artifact = state / "artifacts" / candidate
            base = target["active"]
            predecessor = predecessor_for(config, state, base)
            policy = json.loads(Path(config["compatibility_policy"]).read_text(encoding="utf-8"))
            matrix = run_matrix(artifact, candidate, predecessor, base, policy, python)
            verify_policy_receipt(matrix, candidate, base, policy, predecessor)
            evidence = {"github": gate, "build": build, "compatibility": matrix}
            with TemporaryDirectory(prefix=".package-", dir=records) as directory:
                archive = Path(directory) / "release.tar"
                stage_transfer(artifact, candidate, archive)
                require_protected_tip(settings, commit, read)
                write_json(checked, evidence)
                with archive.open("rb") as source:
                    outcome = operate(config, lambda _: None, checked_main=commit, candidate=candidate,
                                      base=base, source=source)
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
    args = parser.parse_args()
    try:
        outcome = deploy_once(read_operator_config(args.config))
    except (OSError, KeyError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Deploy protected main: {error}\n")
    print(json.dumps(outcome))
    return int(outcome["status"] not in {"released", "unchanged"})


if __name__ == "__main__":
    raise SystemExit(main())
