"""Refresh enabled Taskboard extensions once on an initialized shared ledger."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path

from tasks_core import live_board_url
from tasks_extensions import configuration
from tasks_github import GitHubReader
from tasks_github_extensions import refresh
from tasks_stamp import StampReader, refresh_approvals
from tasks_storage import Postgres, open_sqlite


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    ledger = parser.add_mutually_exclusive_group(required=True)
    ledger.add_argument("--db", type=Path, help="Path to the existing shared SQLite ledger.")
    ledger.add_argument("--postgres", action="store_true", help="Use TASKS_POSTGRES_DSN.")
    parser.add_argument("--active-release", type=Path,
                        help="The operator's active release directory for verified Pi deployment evidence.")
    parser.add_argument("--stamp-pull", help="Read the formal Stamp approvals for this linked GitHub pull request.")
    args = parser.parse_args()
    try:
        configured = configuration(os.environ.get("TASKS_PROJECT_EXTENSIONS"))
        if not configured:
            raise ValueError("Configure enabled projects with TASKS_PROJECT_EXTENSIONS.")
        if args.postgres:
            dsn = os.environ.get("TASKS_POSTGRES_DSN")
            if not dsn:
                raise ValueError("Set TASKS_POSTGRES_DSN in the operator environment.")
            db = Postgres(dsn)
        else:
            path = args.db.expanduser().resolve()
            if not path.is_file():
                raise ValueError("Select an existing shared SQLite ledger.")
            db = open_sqlite(path)
        with closing(db):
            url = live_board_url(db)
            if not url:
                raise ValueError("Initialize the shared board before refreshing its extensions.")
            if args.stamp_pull:
                report = {"stamp": refresh_approvals(db, StampReader(), configured, args.stamp_pull), "errors": []}
            else:
                report = refresh(db, GitHubReader(), configured, release_source=args.active_release, board_url=url)
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"Refresh Taskboard extensions: {error}\n")
    print(json.dumps(report, sort_keys=True))
    return int(bool(report["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
