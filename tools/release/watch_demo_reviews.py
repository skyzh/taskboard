"""Read verified demo comparisons on the Mac and route through signed Taskboard."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import subprocess

from taskboard_client.connection import SharedBoard
from taskboard_server import demo_review


class MacProvider(demo_review.Provider):
    def __init__(self, repository):
        super().__init__(repository, token=None)

    def request(self, path):
        result = subprocess.run(["gh", "api", self.path + path], capture_output=True, timeout=30)
        if result.returncode:
            raise ValueError("GitHub CLI did not confirm the demo comparison read.")
        return result.stdout

    def read(self, path):
        return demo_review.json_object(self.request(path))

    def download(self, attachment):
        return self.request(f"/actions/artifacts/{attachment}/zip")


def poll(provider, board, config, *, since, apply):
    context = board.invoke("context")
    if (not config.board.startswith("https://")
            or context["board"].rstrip("/") != config.board.rstrip("/")
            or context["actor"]["id"] != config.owner):
        raise ValueError("Use the configured HTTPS board and signed comparison operator.")

    def submit(observation):
        return board.invoke(demo_review.SIGNED_ACTION, {
            "observation": json.dumps(observation), "apply": apply,
        }, write=True)["outcome"]

    return demo_review.poll_verified(provider, config, submit, since=since)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", required=True, help="Expected Taskboard HTTPS base.")
    parser.add_argument("--owner", required=True, help="Configured signed project manager ID.")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--baseline", required=True, help="Exact approved demo baseline on protected main.")
    parser.add_argument("--service", default="tasks-board")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--apply", action="store_true", help="Route tickets after confirming the signed preview.")
    args = parser.parse_args()
    if not 1 <= args.hours <= 168 or not demo_review.COMMIT.fullmatch(args.baseline):
        parser.error("Use a full baseline commit and a window from 1 to 168 hours.")
    since = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        result = poll(MacProvider(args.repository), SharedBoard(args.service), args, since=since, apply=args.apply)
    except (KeyError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Taskboard demo review watcher: {error}\n")
    print(json.dumps({"preview": not args.apply, **result}, sort_keys=True))
    return int(bool(result.get("invalid")))


if __name__ == "__main__":
    raise SystemExit(main())
