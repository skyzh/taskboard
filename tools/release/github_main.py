"""Read exact protected main and required check results from public GitHub."""

import fcntl
import json
import re
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from queue_provenance import GIT_COMMIT
from release_plan import ReleaseRejected, write_json


class GitHubRateLimited(OSError):
    def __init__(self, retry_at):
        self.retry_at = retry_at
        super().__init__(f"GitHub REST is rate limited until {retry_at} UTC epoch seconds.")


def rate_limit_deadline(headers, now):
    retry_after = headers.get("Retry-After", "").strip()
    reset = headers.get("X-RateLimit-Reset", "").strip()
    deadline = now
    if retry_after.isascii() and retry_after.isdigit():
        deadline = now + int(retry_after)
    if headers.get("X-RateLimit-Remaining") == "0" and reset.isascii() and reset.isdigit():
        deadline = max(deadline, int(reset))
    return deadline if deadline > now else None


def read_github(path):
    request = Request("https://api.github.com" + path, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urlopen(request, timeout=15) as response:
            return json.load(response)
    except HTTPError as error:
        error.close()
        if error.code in (403, 429) and (deadline := rate_limit_deadline(error.headers, time.time())):
            raise GitHubRateLimited(deadline) from error
        raise


def respect_rate_limit(read, path, *, persist=True):
    """Share one REST retry deadline between the deploy and CI runners."""
    def check_and_read(endpoint):
        if path.is_file():
            retry_at = json.loads(path.read_text(encoding="utf-8"))["retry_at"]
            if time.time() < retry_at:
                raise GitHubRateLimited(retry_at)
        try:
            return read(endpoint)
        except GitHubRateLimited as error:
            if persist:
                write_json(path, {"retry_at": error.retry_at})
            raise

    def guarded_read(endpoint):
        if not persist:
            return check_and_read(endpoint)
        # Serialize one REST request and its deadline, never a build or an intake write.
        with path.with_suffix(".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return check_and_read(endpoint)

    return guarded_read


def read_github_pages(path, *, field, read):
    """Read CI runs or jobs in 100-item pages using the public REST transport."""
    first = read(path)
    total = first["total_count"]
    if type(total) is not int or not 0 <= total <= 1000:
        raise ValueError("GitHub result exceeds the supported 1,000-item window.")
    pages = [first]
    for offset in range(100, total, 100):
        if len(pages[-1][field]) != 100:
            raise ValueError("GitHub returned a short intermediate page.")
        page = read(f"{path}&page={offset // 100 + 1}")
        if page["total_count"] != total:
            raise ValueError("GitHub changed the result count while it was being read.")
        pages.append(page)
    return pages


def repository_path(settings):
    repo = settings["repo"]
    if not isinstance(repo, str) or re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repo) is None:
        raise ValueError("Set github.repo to the reviewed public GitHub owner/repository.")
    return f"/repos/{repo}"


def protected_tip(settings, read):
    branch = read(repository_path(settings) + "/branches/main")
    commit = branch["commit"]["sha"]
    if branch["name"] != "main" or branch["protected"] is not True or GIT_COMMIT.fullmatch(commit) is None:
        raise ReleaseRejected("GitHub did not report a protected main tip.")
    return commit


def require_protected_tip(settings, commit, read):
    if protected_tip(settings, read) != commit:
        raise ReleaseRejected("The requested commit must be the exact protected GitHub main tip.")


def verify_required_checks(settings, commit, read):
    required = settings["required_checks"]
    if not isinstance(required, dict) or not required or any(
        not name or type(app_id) is not int or app_id <= 0 for name, app_id in required.items()
    ):
        raise ValueError("Configure the observed required check names and their GitHub App IDs.")
    repo = repository_path(settings)
    page = read(f"{repo}/commits/{commit}/check-runs?filter=latest&per_page=100")
    if page["total_count"] != len(page["check_runs"]):
        raise ReleaseRejected("GitHub did not return all check results for this commit.")
    checks = {}
    for name, app_id in required.items():
        matches = [run for run in page["check_runs"]
                   if (run["name"], (run.get("app") or {}).get("id")) == (name, app_id)]
        if len(matches) != 1 or (
            matches[0]["head_sha"], matches[0]["status"], matches[0]["conclusion"]
        ) != (commit, "completed", "success"):
            raise ReleaseRejected(f"Required check {name!r} needs a successful result on {commit}.")
        checks[name] = {"app_id": app_id, "run_id": matches[0]["id"]}
    require_protected_tip(settings, commit, read)
    rules = read(repo + "/rules/branches/main")
    policies = [rule["parameters"] for rule in rules if rule["type"] == "required_status_checks"]
    observed = {(check["context"], check.get("integration_id"))
                for policy in policies for check in policy["required_status_checks"]}
    if (observed != set(required.items())
            or not any(policy["strict_required_status_checks_policy"] is True for policy in policies)
            or not any(rule["type"] == "pull_request" for rule in rules)):
        raise ReleaseRejected("The active main rules do not match the verified required-check policy.")
    return {"repo": settings["repo"], "commit": commit, "checks": checks}
