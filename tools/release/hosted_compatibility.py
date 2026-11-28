"""Read an optional public compatibility proof from the verified Portable job."""

import json

from github_main import GitHubRateLimited, repository_path


NOTICE = "Taskboard public compatibility"
STEP = "Qualify public release pairings"
WORKFLOW = ".github/workflows/ci.yml"


def public_compatibility_proof(settings, gate, read):
    """Return a proof only from the current successful attempt of a protected push.

    The caller supplies the result of verify_required_checks and the same
    anonymous GitHub reader. The release matrix still checks applicability to
    its local manifests and policy. No current launcher calls this function.
    """
    repo = repository_path(settings)
    if gate["repo"] != settings["repo"] or "Portable checks" not in gate["checks"]:
        return None
    commit = gate["commit"]
    job_id = gate["checks"]["Portable checks"]["run_id"]
    try:
        return _read_proof(repo, commit, job_id, read)
    except GitHubRateLimited:
        raise
    except (KeyError, OSError, TypeError, ValueError):
        # A successful GitHub check is not itself evidence of qualification.
        return None


def _read_proof(repo, commit, job_id, read):
    job = read(f"{repo}/actions/jobs/{job_id}")
    if (job["id"], job["name"], job["head_sha"], job["head_branch"], job["status"], job["conclusion"],
        job["check_run_url"]) != (
        job_id, "Portable checks", commit, "main", "completed", "success",
        f"https://api.github.com{repo}/check-runs/{job_id}",
    ):
        return None
    selected = [step for step in job["steps"] if step["name"] == STEP]
    if len(selected) != 1 or (selected[0]["status"], selected[0]["conclusion"]) != ("completed", "success"):
        return None

    run_id = job["run_id"]
    if type(run_id) is not int or run_id <= 0:
        return None
    run = read(f"{repo}/actions/runs/{run_id}")
    if (run["id"], run["head_sha"], run["head_branch"], run["event"], run["path"],
        run["run_attempt"], run["head_repository"]["full_name"], run["repository"]["full_name"],
        run["status"], run["conclusion"]) != (
        run_id, commit, "main", "push", WORKFLOW, job["run_attempt"], repo.removeprefix("/repos/"),
        repo.removeprefix("/repos/"), "completed", "success",
    ):
        return None

    annotations = read(f"{repo}/check-runs/{job_id}/annotations?per_page=100")
    if not isinstance(annotations, list) or len(annotations) >= 100:
        return None
    matches = [entry for entry in annotations if isinstance(entry, dict)
               and (entry.get("title"), entry.get("annotation_level")) == (NOTICE, "notice")]
    if len(matches) != 1:
        return None
    message = matches[0].get("message")
    if not isinstance(message, str) or len(message) > 8192:
        return None
    proof = json.loads(message)
    return proof if isinstance(proof, dict) else None
