"""Admit exact, authenticated Tasks release events to an operator's private queue."""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

from ci_watch import check_commit, git, resolve_commit
from local_queue import queue_lock, write_json
from queue_provenance import GIT_COMMIT, ProvenanceRejected, read_reviewed_job
from release_compatibility import run_matrix, verify_policy_receipt
from release_plan import verify_manifest


def verify_changelog_range(repo, state, active_base, changelog):
    if repo is None:
        raise ProvenanceRejected("Pass --repo to verify the changelog's exact Git range.")
    base, head = changelog["base_commit"], changelog["head_commit"]
    try:
        receipt = json.loads((state / "commits" / f"{base}.json").read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ProvenanceRejected("Supply the trusted CI receipt for the active changelog base.") from error
    if (not isinstance(receipt, dict) or receipt.get("status") != "pass"
            or receipt.get("commit") != base or receipt.get("candidate") != active_base):
        raise ProvenanceRejected("The changelog base must match the active release's passing CI receipt.")
    if git(repo, "merge-base", base, head).decode().strip() != base:
        raise ProvenanceRejected("The changelog base must be an ancestor of its release commit.")
    commits = set(git(repo, "rev-list", f"{base}..{head}").decode().splitlines())
    if any(entry["merge_commit"] not in commits for entry in changelog["entries"]):
        raise ProvenanceRejected("Each changelog merge must be inside the selected Git range.")


def enqueue_admitted(invoke, state, queue, active_manifest, commit, submission_id, review_id, reviewers, queue_ticket,
                     *, repo=None, compatibility_policy=None, python=sys.executable):
    state, queue, active_manifest = Path(state), Path(queue), Path(active_manifest)
    if GIT_COMMIT.fullmatch(commit) is None:
        raise ProvenanceRejected("Provide the full checked commit.")
    receipt = json.loads((state / "commits" / f"{commit}.json").read_text(encoding="utf-8"))
    active_base = sha256(active_manifest.read_bytes()).hexdigest()
    job = read_reviewed_job(
        invoke, submission_id=submission_id, review_id=review_id,
        ci_receipt=receipt, active_base=active_base, reviewers=reviewers, queue_ticket=queue_ticket,
    )
    if "changelog" in job:
        verify_changelog_range(repo, state, active_base, job["changelog"])
    artifact = state / "artifacts" / job["candidate"]
    verify_manifest(artifact, job["candidate"])
    if compatibility_policy is not None:
        receipt = run_matrix(artifact, job["candidate"], active_manifest.parent, active_base,
                             compatibility_policy, python)
        verify_policy_receipt(receipt, job["candidate"], active_base, compatibility_policy, active_manifest.parent)
        job["compatibility"] = receipt
    queue.mkdir(mode=0o700, parents=True, exist_ok=True)
    with queue_lock(queue):
        path = queue / (job["release_id"] + ".json")
        if path.exists():
            recorded = json.loads(path.read_text(encoding="utf-8"))
            ignored = {"queued_at", "compatibility"}
            previous = {key: value for key, value in recorded.items() if key not in ignored}
            proposed = {key: value for key, value in job.items() if key not in ignored}
            if previous != proposed:
                raise ProvenanceRejected("The release event was already queued with different receipts.")
            if compatibility_policy is not None:
                verify_policy_receipt(recorded["compatibility"], job["candidate"], active_base,
                                      compatibility_policy, active_manifest.parent)
            return recorded
        job["queued_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        write_json(path, job)
    return job


def authenticated_reader(service):
    def invoke(action, params):
        command = ["raft", "integration", "invoke", "--service", service, "--action", action, "--json"]
        for name, value in params.items():
            command.extend(["--param", f"{name}={value}"])
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise ProvenanceRejected("The signed-in Tasks session could not read the exact receipt.")
        return json.loads(result.stdout)["data"]["result"]

    return invoke


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--active-manifest", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--submission-event", type=int, required=True)
    parser.add_argument("--review-event", type=int, required=True)
    parser.add_argument("--reviewer", action="append", required=True)
    parser.add_argument("--repo", type=Path, help="Verify the changelog range or check a missing local CI receipt.")
    parser.add_argument("--ref", default="refs/heads/main", help="Locally selected ref for an on-demand CI check.")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--compatibility-policy", type=Path, required=True,
                        help="Trusted operator policy with exact older client fixtures.")
    parser.add_argument("--service", default="tasks-board")
    parser.add_argument("--queue-ticket", default="T-78")
    args = parser.parse_args()
    try:
        policy = json.loads(args.compatibility_policy.read_text(encoding="utf-8"))
        if not isinstance(policy, dict):
            raise ProvenanceRejected("Provide a compatibility policy object.")
        if GIT_COMMIT.fullmatch(args.commit) is None:
            raise ProvenanceRejected("Provide the full checked commit.")
        if args.repo and not (args.state / "commits" / f"{args.commit}.json").is_file():
            if resolve_commit(args.repo, args.ref) != args.commit:
                raise ProvenanceRejected("Set the local CI ref to the exact submitted commit.")
            check_commit(args.repo, args.state, args.commit, args.python)
        job = enqueue_admitted(
            authenticated_reader(args.service), args.state, args.queue, args.active_manifest, args.commit,
            args.submission_event, args.review_event, args.reviewer, args.queue_ticket, repo=args.repo,
            compatibility_policy=policy, python=args.python,
        )
    except (OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Release admission failed: {error}\n")
    print(json.dumps(job))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
