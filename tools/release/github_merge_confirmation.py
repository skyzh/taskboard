"""Capture and save one authenticated GitHub merge confirmation for a linked ticket."""

import argparse
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

from queue_provenance import GIT_COMMIT, SHA256, full_hash, ticket_number
from release_plan import write_json

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.checkout import add_packages

add_packages("client", "server")
import tasks_core as ledger
import tasks_extensions as extensions
from tasks_github import _time, merge_pull, repository
import tasks_projects as projects
import tasks_storage
from watches import linked_sources


SCHEMA = "taskboard-github-merge-confirmation/v1"


def capture(item, commit, *, run=subprocess.run):
    command = ["gh", "pr", "view", item.number, "--repo", f"github.com/{repository(item)}",
               "--json", "number,url,state,mergeCommit,mergedAt"]
    response = run(command, check=True, capture_output=True, timeout=20)
    data = json.loads(response.stdout)
    if (not isinstance(data, dict) or type(data.get("number")) is not int
            or not isinstance(data.get("url"), str)
            or (data["number"], data["url"].lower(), data.get("state")) != (int(item.number), item.url, "MERGED")):
        raise ValueError("GitHub did not confirm the selected merged pull request.")
    landed = data.get("mergeCommit")
    at = _time(data.get("mergedAt"))
    if not isinstance(landed, dict) or landed.get("oid") != commit or at is None:
        raise ValueError("GitHub did not return the exact landed SHA and merge timestamp.")
    return {"schema": SCHEMA, "pull": item.url, "merge_commit": commit, "merged_at": at}


def save(db, board, ticket, item, commit, receipt, configured):
    if (receipt.keys() != {"schema", "pull", "merge_commit", "merged_at"}
            or (receipt["schema"], receipt["pull"], receipt["merge_commit"]) != (SCHEMA, item.url, commit)
            or not isinstance(receipt["merged_at"], str) or _time(receipt["merged_at"]) != receipt["merged_at"]):
        raise ValueError("Use the reviewed confirmation for the exact pull and SHA.")
    with tasks_storage.write(db):
        saved_board = ledger.live_board_url(db)
        if saved_board is None or saved_board.rstrip("/") != board.rstrip("/"):
            raise ValueError("Select the initialized ledger of the intended board.")
        task_id = projects.resolve(db, ticket)
        task = ledger.get_work_task(db, task_id)
        if projects.row_reference(task) != ticket:
            raise ValueError("Use the current canonical work ticket ID.")
        config = configured.get(projects.row_project(task), {})
        rule = config.get(extensions.EVENTS)
        if not (config.get(extensions.STATUS) or rule and rule.repository == repository(item)):
            raise ValueError("Enable the existing GitHub extension for this project first.")
        sources = [source["id"] for source in linked_sources(db, task_id)
                   if source["kind"] == "github" and source["target"].lower().rstrip("/") == item.url]
        if len(sources) != 1:
            raise ValueError("Choose one pull request already linked as a GitHub source on this ticket.")
        source, = sources
        at = receipt["merged_at"]
        select = "SELECT state, merge_commit, merged_at FROM github_source_status WHERE source_id = ?"
        existing = db.execute(select, (source,)).fetchone()
        if existing is not None:
            if existing["merge_commit"] not in (None, commit) or _time(existing["merged_at"]) not in (None, at):
                raise ValueError("The source already has different merge confirmation.")
            if tuple(existing) == ("merged", commit, at):
                return "already_saved"
        extensions.save_status(db, source, "merged", ledger.timestamp(), merge_commit=commit, merged_at=at)
        if tuple(db.execute(select, (source,)).fetchone()) != ("merged", commit, at):
            raise ValueError("The merge confirmation did not persist.")
        ledger.bump_revision(db)
        return "saved"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--pull", required=True)
    parser.add_argument("--merge-commit", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("capture", help="Read one pull through the existing authenticated GitHub CLI.")
    fetch.add_argument("--output", type=Path, required=True)
    importer = commands.add_parser("save", help="Save only the reviewed confirmation through the board operator.")
    importer.add_argument("--db", type=Path, required=True)
    importer.add_argument("--board", required=True)
    importer.add_argument("--ticket", required=True)
    importer.add_argument("--receipt", type=Path, required=True)
    importer.add_argument("--sha256", required=True)
    args = parser.parse_args(argv)
    try:
        item = merge_pull(args.pull)
        commit = full_hash({"commit": args.merge_commit}, "commit", GIT_COMMIT)
        if args.command == "capture":
            if args.output.exists():
                raise ValueError("Use a new private receipt path.")
            receipt = capture(item, commit)
            write_json(args.output, receipt)
            result = {"status": "captured", "sha256": sha256(args.output.read_bytes()).hexdigest(), **receipt}
        else:
            digest = full_hash({"sha256": args.sha256}, "sha256", SHA256)
            ticket_number(args.ticket)
            raw = args.receipt.read_bytes()
            if sha256(raw).hexdigest() != digest:
                raise ValueError("The receipt does not match the reviewed digest.")
            data = json.loads(raw)
            if not isinstance(data, dict) or not args.db.is_file():
                raise ValueError("Choose a confirmation file and an existing initialized SQLite ledger.")
            configured = extensions.configuration(os.environ.get("TASKS_PROJECT_EXTENSIONS"))
            with closing(tasks_storage.open_sqlite(args.db)) as db:
                status = save(db, args.board, args.ticket, item, commit, data, configured)
            result = {"status": status, "pull": item.url, "ticket": args.ticket, "merge_commit": commit}
    except (KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError):
        # Provider output and operator paths can contain private context.
        parser.exit(1, "Merge confirmation refused. Check the exact pull, reviewed receipt, ledger and existing access.\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
