"""Read optional public compatibility evidence from the verified main CI run."""

import json

from github_main import GitHubRateLimited, repository_path
from queue_provenance import GIT_COMMIT


PORTABLE = "Portable checks"
QUALIFICATION = "Public release qualification"
STEP = "Qualify public release pairings"
NOTICE = "Taskboard public compatibility"
WORKFLOW = ".github/workflows/ci.yml"


def public_compatibility_proof(settings, gate, read, *, predecessor_commit):
    """Return the proof for a trusted gate and the mounted predecessor commit.

    The caller supplies the exact result of verify_required_checks and the
    same anonymous GitHub reader. The release matrix still verifies the
    proof against both local manifest digests and the active policy.
    """
    repo = repository_path(settings)
    try:
        commit = gate["commit"]
        if (gate["repo"] != settings["repo"] or commit == predecessor_commit
                or any(not isinstance(sha, str) or GIT_COMMIT.fullmatch(sha) is None
                       for sha in (commit, predecessor_commit))):
            return None
        portable = gate["checks"][PORTABLE]
        job_id, app_id = portable["run_id"], portable["app_id"]
        if not all(type(value) is int and value > 0 for value in (job_id, app_id)):
            return None
        return _read_proof(repo, commit, predecessor_commit, job_id, app_id, read)
    except GitHubRateLimited:
        raise
    except (KeyError, OSError, RecursionError, TypeError, ValueError):
        return None


def _successful_job(job, repo, name, commit, job_id):
    return (job["id"], job["name"], job["head_sha"], job["head_branch"], job["status"], job["conclusion"],
            job["check_run_url"]) == (job_id, name, commit, "main", "completed", "success",
                                      f"https://api.github.com{repo}/check-runs/{job_id}")


def _read_proof(repo, commit, predecessor_commit, job_id, app_id, read):
    portable = read(f"{repo}/actions/jobs/{job_id}")
    if not _successful_job(portable, repo, PORTABLE, commit, job_id):
        return None
    run_id, attempt = portable["run_id"], portable["run_attempt"]
    if not all(type(value) is int and value > 0 for value in (run_id, attempt)):
        return None

    run = read(f"{repo}/actions/runs/{run_id}")
    repo_name = repo.removeprefix("/repos/")
    if (run["id"], run["head_sha"], run["head_branch"], run["event"], run["path"], run["run_attempt"],
        run["head_repository"]["full_name"], run["repository"]["full_name"], run["status"], run["conclusion"]) != (
        run_id, commit, "main", "push", WORKFLOW, attempt, repo_name, repo_name, "completed", "success",
    ):
        return None

    page = read(f"{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
    jobs = page["jobs"]
    if not isinstance(jobs, list) or page["total_count"] != len(jobs) or len(jobs) >= 100:
        return None
    matching = [job for job in jobs if job["name"] == QUALIFICATION]
    if len(matching) != 1:
        return None
    job = matching[0]
    proof_job_id = job["id"]
    if (type(proof_job_id) is not int or proof_job_id <= 0 or proof_job_id == job_id
            or (job["run_id"], job["run_attempt"]) != (run_id, attempt)
            or not _successful_job(job, repo, QUALIFICATION, commit, proof_job_id)):
        return None
    steps = [step for step in job["steps"] if step["name"] == STEP]
    if len(steps) != 1 or (steps[0]["status"], steps[0]["conclusion"]) != ("completed", "success"):
        return None

    check_url = f"{repo}/check-runs/{proof_job_id}"
    check = read(check_url)
    if (check["id"], check["name"], check["head_sha"], check["app"]["id"], check["status"],
        check["conclusion"]) != (proof_job_id, QUALIFICATION, commit, app_id, "completed", "success"):
        return None
    return _read_notice(read(f"{check_url}/annotations?per_page=100"), commit, predecessor_commit)


def _read_notice(annotations, commit, predecessor_commit):
    if not isinstance(annotations, list) or len(annotations) >= 100:
        return None
    matches = [entry for entry in annotations if isinstance(entry, dict)
               and (entry.get("title"), entry.get("annotation_level")) == (NOTICE, "notice")]
    if len(matches) != 1:
        return None
    message = matches[0].get("message")
    if not isinstance(message, str) or len(message) > 8192:
        return None
    envelope = json.loads(message)
    if not isinstance(envelope, dict) or (envelope.get("after"), envelope.get("before")) != (commit, predecessor_commit):
        return None
    proof = envelope.get("proof")
    return proof if isinstance(proof, dict) else None
