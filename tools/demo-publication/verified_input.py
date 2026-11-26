"""Resolve a completed PR comparison cue against GitHub's current pull request."""

import json
import os
from pathlib import Path
import re
from urllib.parse import urlencode
from github_read import read

CHECK_WORKFLOW = ".github/workflows/summary-demo-check.yml"
COMMIT = re.compile(r"[0-9a-f]{40}")


def selected_pull(event, repository, read):
    cue = event["workflow_run"]
    if (event["action"] != "completed" or cue["event"] != "pull_request"
            or cue["path"] != CHECK_WORKFLOW or cue["repository"]["full_name"] != repository):
        return None
    remote = cue.get("head_repository")
    if not remote:
        return None
    linked = cue["pull_requests"]
    numbers = {item["number"] for item in linked if item["base"]["ref"] == "main"}
    if not linked:
        # GitHub can omit linked pull requests for fork-origin runs.
        query = urlencode({"state": "all", "base": "main", "per_page": 100,
                           "head": f"{remote['owner']['login']}:{cue['head_branch']}"})
        numbers = {item["number"] for item in read(f"/pulls?{query}") if item["head"]["sha"] == cue["head_sha"]}
    matches = []
    for number in numbers:
        if type(number) is not int or number < 1:
            continue
        pull = read(f"/pulls/{number}")
        if current_match(pull, cue, repository, number):
            matches.append((number, pull))
    if len(matches) != 1:
        return None
    number, pull = matches[0]
    return {
        "pull": number, "head": pull["head"]["sha"], "base": pull["base"]["sha"],
        "candidate": pull["merge_commit_sha"], "source_run": cue["id"],
    }


def current_match(pull, cue, repository, number):
    return (
        pull["number"] == number and pull["base"]["ref"] == "main"
        and pull["base"]["repo"]["full_name"] == repository
        and pull["head"]["ref"] == cue["head_branch"]
        and (pull["head"].get("repo") or {}).get("id") == cue["head_repository"]["id"]
        and pull["head"]["sha"] == cue["head_sha"]
        and (pull["state"] == "open" or pull.get("merged") is True)
        and all(isinstance(sha, str) and COMMIT.fullmatch(sha)
                for sha in (pull["head"]["sha"], pull["base"]["sha"], pull.get("merge_commit_sha")))
    )


if __name__ == "__main__":
    repository = os.environ["GITHUB_REPOSITORY"]
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid workflow repository.")

    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    selection = selected_pull(event, repository, lambda path: read(f"/repos/{repository}{path}"))
    with open(os.environ["GITHUB_OUTPUT"], "a") as target:
        for field, value in (selection or {}).items():
            print(f"{field}={value}", file=target)
    if selection is None:
        print("The comparison cue has no current pull request on protected main.")
