"""Test exact local main and release it through the existing Pi operator."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory

from ci_watch import check_commit, resolve_commit
from local_queue import elapsed_ms, queue_lock, write_json
from publish_receipt import HEADLINES, publish_outcome, signed_in_service
from queue_provenance import GIT_COMMIT, ticket_number
from release_compatibility import run_matrix, verify_policy_receipt
from release_plan import ReleaseRejected, file_hash
from stage_transfer import stage_transfer


def ssh_operator(ssh, operator):
    def remote(args, *, archive=None):
        command = [*ssh, shlex.join([*operator, *args])]
        if archive is not None:
            with archive.open("rb") as source:
                result = subprocess.run(command, stdin=source, capture_output=True, check=True, timeout=300)
        else:
            result = subprocess.run(command, input=b"", capture_output=True, check=True, timeout=45)
        return json.loads(result.stdout)
    return remote


def require_main(repo, commit):
    if resolve_commit(repo, "refs/heads/main") != commit:
        raise ReleaseRejected("The requested commit must still be the exact local main tip.")


def check_pi_outcome(config, commit, result, checked):
    ticket = config.get("queue_ticket", "T-78")
    candidate, base = checked["candidate"], checked["base"]
    expected = (f"release-main-{commit}", ticket, commit, candidate, base)
    if tuple(result.get(key) for key in ("release_id", "ticket", "commit", "candidate", "base")) != expected:
        raise ValueError("The Pi outcome does not match the checked deployment inputs.")
    if result["status"] not in HEADLINES or (result["status"] == "released" and result["active"] != candidate):
        raise ValueError("The Pi did not confirm an exact release outcome.")
    return {**result, "evidence": checked["evidence"],
            "elapsed_ms": elapsed_ms(checked["started_at"], datetime.fromisoformat(result["at"]))}


def predecessor_for(config, state, base):
    if not isinstance(base, str) or re.fullmatch("[0-9a-f]{64}", base) is None:
        raise ValueError("The Pi did not report a full active manifest digest.")
    cached = state / "artifacts" / base
    predecessor = cached if cached.is_dir() else Path(config["predecessor"])
    if file_hash(predecessor / "SHA256SUMS") != base:
        raise ReleaseRejected("Stage the Pi's exact active app snapshot for compatibility.")
    return predecessor.resolve(strict=True)


def check_and_transfer(config, commit, python, remote, scratch, checked_path):
    repo, state = Path(config["repo"]), Path(config["ci_state"])
    ticket = config.get("queue_ticket", "T-78")
    started = datetime.now(timezone.utc)
    step, candidate = "main_ref", None
    evidence = {}
    try:
        require_main(repo, commit)
        step = "ci"
        ci = check_commit(repo, state, commit, python)
        evidence["ci"] = ci
        if (ci.get("commit"), ci.get("status")) != (commit, "pass"):
            failed = ", ".join(name for name, check in ci.get("checks", {}).items() if check["status"] == "fail")
            raise ReleaseRejected(f"CI failed for this commit ({failed or 'no passing receipt'}).")
        candidate = ci["candidate"]
        artifact = state / "artifacts" / candidate
        step = "read_active"
        live = remote(["--checked-main", commit, "--read-state"])
        if live["outcome"] is not None:
            raise ValueError("Use the original build operator state to reconcile this Pi outcome.")
        step = "compatibility"
        base = live["active"]
        predecessor = predecessor_for(config, state, base)
        policy = json.loads(Path(config["compatibility_policy"]).read_text(encoding="utf-8"))
        matrix = run_matrix(artifact, candidate, predecessor, base, policy, python)
        verify_policy_receipt(matrix, candidate, base, policy, predecessor)
        evidence["compatibility"] = matrix
        step = "artifact"
        archive = scratch / "release.tar"
        stage_transfer(artifact, candidate, archive)
        step = "main_ref"
        require_main(repo, commit)
    except (OSError, KeyError, ValueError, subprocess.SubprocessError) as error:
        if step == "read_active":
            raise
        now = datetime.now(timezone.utc)
        # A failed Mac check has no selected release to import into the active-version ledger.
        return {"release_id": f"release-main-{commit}", "ticket": ticket, "commit": commit,
                "status": "rejected", "candidate": None, "reason_code": step, "reason": str(error),
                "at": now.isoformat(timespec="milliseconds"), "evidence": evidence,
                "elapsed_ms": elapsed_ms(started.isoformat(), now)}

    checked = {"candidate": candidate, "base": base, "evidence": evidence, "started_at": started.isoformat()}
    write_json(checked_path, checked)
    result = remote(["--checked-main", commit, "--candidate", candidate, "--base", base], archive=archive)
    return check_pi_outcome(config, commit, result, checked)


def run_main(config, commit, python, remote, invoke):
    if GIT_COMMIT.fullmatch(commit) is None:
        raise ValueError("Provide the full main commit.")
    ticket_number(config.get("queue_ticket", "T-78"))
    board = invoke("context", {})["board"].rstrip("/")
    if not board.startswith("https://") or board != config["mount"].rstrip("/"):
        raise ValueError("The signed-in Activity service must match the configured HTTPS board.")
    state = Path(config["ci_state"]) / "main-deployments"
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    receipt = state / f"{commit}.json"
    checked_path = receipt.with_suffix(".checked")
    with queue_lock(state):
        if receipt.is_file():
            outcome = json.loads(receipt.read_text(encoding="utf-8"))
        else:
            saved = remote(["--checked-main", commit, "--read-state"])["outcome"] if checked_path.is_file() else None
            if saved is not None:
                checked = json.loads(checked_path.read_text(encoding="utf-8"))
                outcome = check_pi_outcome(config, commit, saved, checked)
            else:
                with TemporaryDirectory(prefix=".package-", dir=state) as directory:
                    outcome = check_and_transfer(config, commit, python, remote, Path(directory), checked_path)
            write_json(receipt, outcome)
        event = publish_outcome(invoke, outcome, confirm=True)["event_id"]
        url = f"{board}/#event-{event}-log"
        projection = None
        if outcome["candidate"] is not None:
            projection = remote(["--checked-main", commit, "--activity-url", url])
            if (projection.get("release_id"), projection.get("status")) != (outcome["release_id"], outcome["status"]):
                raise ValueError("The active-version ledger did not confirm this release outcome.")
        return {**outcome, "activity_url": url, "projection": projection}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Private build operator configuration.")
    parser.add_argument("--commit", required=True, help="Full exact local main commit.")
    parser.add_argument("--record-service", required=True, help="Existing signed-in Taskboard service.")
    args = parser.parse_args()
    try:
        metadata = args.config.stat()
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
            raise ValueError("Use a build operator configuration with private write access.")
        config = json.loads(args.config.read_text(encoding="utf-8"))
        outcome = run_main(config, args.commit, config.get("python", sys.executable),
                           ssh_operator(config["ssh"], config["operator"]), signed_in_service(args.record_service))
    except (OSError, KeyError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Test and deploy main: {error}\n")
    print(json.dumps(outcome))
    return int(outcome["status"] != "released")


if __name__ == "__main__":
    raise SystemExit(main())
