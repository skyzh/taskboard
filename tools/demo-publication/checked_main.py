"""Use the same protected-main policy as the Taskboard host."""

from pathlib import Path
import os
import sys
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "release"))

from github_main import protected_tip, read_github, repository_path, verify_required_checks  # noqa: E402

CHECKS = {"Portable checks": 15368, "SQLite and PostgreSQL preference replay": 15368}


def allowed(repository, revision, *, automatic=False, read=read_github):
    settings = {"repo": repository, "required_checks": CHECKS}
    if protected_tip(settings, read) != revision:
        return False
    verify_required_checks(settings, revision, read)
    if automatic and already_published(repository_path(settings), revision, read):
        print("This exact revision already has a successful Pages deployment.")
        return False
    return True


def already_published(repo, revision, read):
    query = urlencode({"sha": revision, "environment": "github-pages", "per_page": 10})
    for deployment in read(f"{repo}/deployments?{query}"):
        if deployment["sha"] != revision or deployment["environment"] != "github-pages":
            continue
        states = read(f"{repo}/deployments/{deployment['id']}/statuses?per_page=1")
        if states and states[0]["state"] == "success" and states[0].get("environment_url", "").startswith("https://"):
            return True
    return False


if __name__ == "__main__":
    current = allowed(os.environ["GITHUB_REPOSITORY"], os.environ["REVISION"],
                      automatic=os.environ["GITHUB_EVENT_NAME"] == "workflow_run")
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        print(f"current={str(current).lower()}", file=output)
    print("Verified protected main." if current else "Skipped this Pages publication.")
