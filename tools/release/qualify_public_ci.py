"""Prepare a public qualification document from one protected main push."""

import argparse
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys

from ci_watch import build_checked_commit, resolve_commit
from queue_provenance import GIT_COMMIT
from release_compatibility import run_public_matrix


MAIN = "refs/heads/main"
NODE_VERSION = "24.22.3"
VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){2,3}")


def protected_pair(environment, event):
    if (environment.get("GITHUB_EVENT_NAME"), environment.get("GITHUB_REF"),
            environment.get("GITHUB_REF_PROTECTED")) != ("push", MAIN, "true"):
        raise ValueError("Public qualification requires a protected main push.")
    candidate, predecessor = event.get("after"), event.get("before")
    if (event.get("ref") != MAIN or candidate != environment.get("GITHUB_SHA")
            or event.get("forced") is not False or event.get("created") is not False
            or event.get("deleted") is not False
            or not all(isinstance(commit, str) and GIT_COMMIT.fullmatch(commit) and commit != "0" * 40
                       for commit in (candidate, predecessor))
            or candidate == predecessor):
        raise ValueError("Use the exact distinct after and before commits of an ordinary protected push.")
    return candidate, predecessor


def qualify(repo, state, environment, event, policy, *, chromium, python_version, npm_version):
    candidate, predecessor = protected_pair(environment, event)
    if resolve_commit(repo, "HEAD") != candidate:
        raise ValueError("The checkout must be the event candidate.")
    subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", predecessor, candidate],
                   check=True, capture_output=True, timeout=15)
    if not all(VERSION.fullmatch(version) for version in (chromium, python_version, npm_version)):
        raise ValueError("Provide exact numeric Python, npm and Chromium versions.")
    if platform.python_version() != python_version:
        raise ValueError("The qualification Python version does not match its pin.")
    for command, expected in (("node", f"v{NODE_VERSION}"), ("npm", npm_version)):
        observed = subprocess.check_output([command, "--version"], text=True, timeout=10).strip()
        if observed != expected:
            raise ValueError(f"The qualification {command} version does not match its pin.")

    # Use the checkout's pinned Node for the package build and browser fixture.
    policy = {field: policy[field] for field in ("playwright_module", "browser_executable") if field in policy}
    if "playwright_module" not in policy:
        raise ValueError("Provide a staged disposable Playwright module.")
    current, previous = [build_checked_commit(repo, state, commit, sys.executable)["candidate"]
                         for commit in (candidate, predecessor)]
    artifacts = state / "artifacts"
    proof = run_public_matrix(artifacts / current, current, artifacts / previous, previous, policy)
    if proof["browser"]["chromium"] != chromium:
        raise ValueError("The qualification browser version does not match its pin.")
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."), help="Local checkout of the protected push.")
    parser.add_argument("--state", type=Path, required=True, help="Disposable build state outside the checkout.")
    parser.add_argument("--policy", type=Path, required=True, help="Disposable browser policy file.")
    parser.add_argument("--chromium", required=True, help="Expected exact browser version.")
    parser.add_argument("--python-version", required=True, help="Expected exact Python version.")
    parser.add_argument("--npm-version", required=True, help="Expected exact npm version.")
    args = parser.parse_args()
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        policy = json.loads(args.policy.read_text(encoding="utf-8"))
        if not isinstance(event, dict) or not isinstance(policy, dict):
            raise ValueError("Provide JSON objects for the push event and disposable browser policy.")
        proof = qualify(args.repo, args.state, os.environ, event, policy, chromium=args.chromium,
                        python_version=args.python_version, npm_version=args.npm_version)
    except (OSError, KeyError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Public compatibility was not qualified: {error}\n")
    print(json.dumps(proof, separators=(",", ":")))


if __name__ == "__main__":
    main()
