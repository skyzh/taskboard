"""Route current demo comparison results through an operator-owned board connection."""

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener, urlopen
from zipfile import BadZipFile, ZipFile

import tasks_core as ledger
from tasks_github import github_request, link_github_source
import tasks_profiles as profiles
import tasks_projects as projects
import tasks_storage as storage
import watches


WORKFLOW = ".github/workflows/summary-demo-verify.yml"
COMMIT = re.compile(r"[0-9a-f]{40}")
REPO = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .:_/()-]{0,119}")
ARCHIVE_LIMIT = 128 * 1024
JSON_LIMIT = 64 * 1024
RESULTS = {"pass", "changed", "privacy_failed", "unavailable", "error"}
SCREEN = re.compile(r"[A-Za-z0-9_-]{1,100}\.png")


class ArtifactRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        target = urlsplit(new_url)
        if target.scheme != "https" or not target.hostname or target.username is not None:
            raise ValueError("GitHub returned an invalid artifact download URL.")
        next_request = super().redirect_request(request, fp, code, message, headers, new_url)
        if next_request is not None:
            next_request.remove_header("Authorization")
        return next_request


def json_object(raw):
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError("A demo comparison response must be a JSON object.")
    return document


class Provider:
    def __init__(self, repository, token):
        if not REPO.fullmatch(repository):
            raise ValueError("Set the exact GitHub owner/repository for the demo.")
        self.repo = repository
        self.path = f"/repos/{repository}"
        self.token = token

    def read(self, path):
        request = github_request(self.path + path)
        request.add_unredirected_header("Authorization", f"Bearer {self.token}")
        with urlopen(request, timeout=15) as response:
            return json_object(response.read(4 * 1024 * 1024 + 1))

    def runs(self, **filters):
        """Read all pages for the explicitly bounded recent workflow window."""
        path = f"/actions/workflows/{WORKFLOW.rsplit('/', 1)[1]}/runs"
        for page in range(1, 11):
            query = urlencode({"event": "workflow_run", "per_page": 100, "page": page, **filters})
            response = self.read(f"{path}?{query}")
            if response["total_count"] > 1000:
                raise ValueError("Narrow the demo workflow lookup window.")
            rows = response["workflow_runs"]
            yield from rows
            if len(rows) < 100 or page * 100 >= response["total_count"]:
                return

    def named_artifact(self, run, kind):
        name = f"summary-demo-{kind}-{run['run_attempt']}"
        query = urlencode({"name": name})
        response = self.read(f"/actions/runs/{run['id']}/artifacts?{query}")
        entries = response["artifacts"]
        if not entries:
            return None
        if len(entries) != 1:
            raise ValueError("Expected one artifact for the exact comparison attempt.")
        entry = entries[0]
        if entry["expired"] or entry["name"] != name or type(entry["id"]) is not int:
            return None
        return entry

    def has_evidence(self, run, receipt):
        attachment = self.named_artifact(run, "evidence")
        return bool(attachment and attachment["id"] == receipt)

    def artifact(self, run):
        entry = self.named_artifact(run, "comparison")
        if entry is None:
            return None
        attachment = entry["id"]
        if entry["size_in_bytes"] > ARCHIVE_LIMIT:
            raise ValueError("The comparison archive is over the evidence size limit.")
        request = github_request(f"{self.path}/actions/artifacts/{attachment}/zip")
        request.add_unredirected_header("Authorization", f"Bearer {self.token}")
        with build_opener(ArtifactRedirect()).open(request, timeout=30) as response:
            raw = response.read(ARCHIVE_LIMIT + 1)
        if len(raw) > ARCHIVE_LIMIT or sha256(raw).hexdigest() != str(entry["digest"]).removeprefix("sha256:"):
            raise ValueError("The downloaded comparison archive disagrees with GitHub's digest.")
        with ZipFile(BytesIO(raw)) as archive:
            return archive_json(archive, "receipt.json")


def archive_json(archive, filename):
    matches = [member for member in archive.infolist() if member.filename == filename]
    if len(matches) != 1 or matches[0].file_size > JSON_LIMIT:
        raise ValueError(f"Expected one bounded {filename} in the demo evidence.")
    return json_object(archive.read(matches[0]))


def identity(run):
    return run["id"], run["run_attempt"]


def sequence(run):
    return run["run_number"], run["run_attempt"]


def from_workflow(run, repo):
    return (
        run.get("path") == WORKFLOW
        and run.get("event") == "workflow_run" and run.get("head_branch") == "main"
        and all((run.get(field) or {}).get("full_name", "").lower() == repo.lower()
                for field in ("repository", "head_repository"))
    )


def matches_receipt(run, report, repo):
    return (
        (report.get("run"), report.get("attempt")) == identity(run)
        and report.get("repository", "").lower() == repo.lower()
        and type(report.get("pull")) is int and report["pull"] > 0
        and type(report.get("source_run")) is int and report["source_run"] > 0
        and report.get("verifier") == run["head_sha"]
        and all(isinstance(report.get(key), str) and COMMIT.fullmatch(report[key])
                for key in ("verifier", "candidate", "head", "base"))
        and report.get("status") in RESULTS
    )


def current_pull(report, pull, repo):
    return (
        pull.get("number") == report["pull"]
        and pull["base"]["repo"]["full_name"].lower() == repo.lower()
        and pull["base"]["ref"] == "main" and pull["head"]["sha"] == report["head"]
        and (
            pull.get("merged") is True
            or (pull.get("state") == "open" and pull["base"]["sha"] == report["base"]
                and pull.get("merge_commit_sha") == report["candidate"])
        )
    )


def surface_labels(details):
    screens = details.get("changed_screens")
    failures = details.get("interaction_failures")
    if not isinstance(screens, list) or not isinstance(failures, list):
        raise ValueError("The comparison is missing its changed screens or interaction results.")
    if not (screens or failures) or not all(isinstance(item, str) and SCREEN.fullmatch(item) for item in screens):
        raise ValueError("The comparison must name the affected screens.")
    steps = []
    for row in failures:
        if (not isinstance(row, dict) or row.get("view") not in ("phone", "desktop")
                or not isinstance(row.get("step"), str) or not LABEL.fullmatch(row["step"])):
            raise ValueError("The comparison must name the affected steps.")
        steps.append(f"{row['view']}: {row['step']}")
    return list(dict.fromkeys([*steps, *screens]))


def previous_ticket(db, project, url):
    rows = db.execute("""
        SELECT events.task_id, events.context FROM events
        JOIN tasks ON tasks.id = events.task_id
        JOIN task_sources ON task_sources.task_id = tasks.id
        JOIN sources ON sources.id = task_sources.source_id
        WHERE tasks.project_prefix = ? AND sources.kind = 'github'
          AND LOWER(RTRIM(sources.target, '/')) = ? AND events.action IN ('add', 'log')
        ORDER BY events.id DESC
        """, (project, url.lower()))
    for row in rows:
        marker = ledger.event_context(row).get("demo_comparison")
        if marker and marker["pull"] == url:
            return row["task_id"], tuple(marker["attempt"])
    return None, ()


def apply(db, config, run, report, attachment, labels, *, dry_run=False):
    repo, pull = config.repository, report["pull"]
    url = f"https://github.com/{repo}/pull/{pull}"
    run_id, _ = identity(run)
    order = sequence(run)
    run_url = f"https://github.com/{repo}/actions/runs/{run_id}"
    links = [("GitHub pull request", url)]
    if labels:
        links.append(("Demo comparison and screenshots", f"{run_url}/artifacts/{attachment}"))
        note = "Review the demo comparison for " + ", ".join(labels[:12]) + "."
        brief = "Open the comparison evidence, reproduce the affected steps and repair the demo or review a new baseline."
    else:
        links.append(("Passing demo comparison", run_url))
        note = "The current demo comparison passed."
        brief = "Review the passing result and close the demo follow-up if it is resolved."
    context = {
        "demo_comparison": {"pull": url, "attempt": list(order)},
        "activity_details": f"GitHub PR #{pull}, head {report['head']}. {brief}",
    }
    transaction = storage.read if dry_run else storage.write
    with transaction(db):
        project = projects.require(db, config.project)
        if not ledger.live_board_url(db) or not projects.manages(db, project, config.actor):
            raise PermissionError("The route needs an initialized board and an existing project manager.")
        ticket, seen = previous_ticket(db, project, url)
        if order <= seen:
            return "duplicate"
        if not labels and ticket is None:
            return "pass"
        owner = next((config.owners[label] for label in labels if label in config.owners), config.fallback)
        if ticket is None:
            verified = profiles.visible_by_keys(db, [owner], ledger.live_board_url(db))
            if owner not in verified and owner not in projects.roster_owners(db, config.roster):
                raise ValueError("The review owner must be an existing verified board assignee.")
        if dry_run:
            return "would_update" if ticket else "would_create"
        if ticket is None:
            args = argparse.Namespace(
                title=f"Review Taskboard demo changes from PR #{pull}", step=note, brief=brief,
                project=project, by=config.actor, dri=owner,
            )
            ticket = ledger.create_task_record(db, args, links, context=context)
            _, canonical, _ = link_github_source(db, ticket, url)
            watches.link_reference(db, ticket, canonical, "GitHub pull request")
            action = "created"
        else:
            task = ledger.get_task(db, ticket)
            if labels and task["state"] == "done":
                args = argparse.Namespace(command="reopen", id=ticket, by=config.actor, dri=None, note=note)
                ledger.change_ownership(db, args, links)
                task = ledger.get_task(db, ticket)
            ledger.record(db, ticket, "log", config.actor, task["state"], note, None, links, context)
            action = "updated" if labels else "pass_recorded"
        ledger.bump_revision(db)
        return action


def read_verdict(provider, config, run):
    if run["status"] != "completed":
        return "pending", None
    report = provider.artifact(run)
    if report is None:
        return "missing", None
    if not matches_receipt(run, report, config.repository):
        raise ValueError("The comparison receipt does not match its protected workflow run.")
    if report["status"] not in ("pass", "changed"):
        return report["status"], None
    if run["conclusion"] != "success" or report["baseline"] != config.baseline:
        return "unverified", None
    return "verified", report


def route(db, provider, config, run, report, *, dry_run):
    pull = provider.read(f"/pulls/{report['pull']}")
    if not current_pull(report, pull, config.repository):
        return "superseded"
    evidence, labels = None, ()
    if report["status"] == "changed":
        evidence = report.get("evidence")
        if type(evidence) is not int or not 0 < evidence < 10**19 or not provider.has_evidence(run, evidence):
            raise ValueError("The changed comparison must identify screenshots from its own attempt.")
        labels = surface_labels(report["details"])
    return apply(db, config, run, report, evidence, labels, dry_run=dry_run)


def poll(db, provider, config, *, since, dry_run):
    results = Counter()
    current = {}
    for run in provider.runs(created=">=" + since):
        if not from_workflow(run, config.repository):
            continue
        try:
            outcome, report = read_verdict(provider, config, run)
        except (ValueError, KeyError, TypeError, BadZipFile):
            outcome, report = "invalid", None
        if report is None:
            results[outcome] += 1
            continue
        key = report["pull"], report["head"]
        previous = current.get(key)
        if previous is not None:
            results["superseded"] += 1
        if previous is None or sequence(run) > sequence(previous[0]):
            current[key] = (run, report)
    for run, report in current.values():
        try:
            outcome = route(db, provider, config, run, report, dry_run=dry_run)
        except (ValueError, KeyError, TypeError):
            outcome = "invalid"
        results[outcome] += 1
    return dict(results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    board = parser.add_mutually_exclusive_group(required=True)
    board.add_argument("--db", type=Path)
    board.add_argument("--postgres", action="store_true")
    parser.add_argument("--commit", action="store_true", help="Apply changes after checking the preview.")
    parser.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()
    try:
        config = argparse.Namespace(**json_object(os.environ["TASKS_DEMO_REVIEW"]))
        config.roster = projects.parse_roster(os.environ.get("TASKS_PROJECT_ROSTER"))
        if not 1 <= args.hours <= 168 or not COMMIT.fullmatch(config.baseline):
            raise ValueError("Use a full reviewed baseline commit and a window from 1 to 168 hours.")
        provider = Provider(config.repository, os.environ["TASKS_DEMO_ACTIONS_TOKEN"])
        db = storage.Postgres(os.environ["TASKS_POSTGRES_DSN"]) if args.postgres else storage.open_sqlite(
            args.db.expanduser().resolve(), write_existing=args.commit
        )
        since = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with closing(db):
            result = poll(db, provider, config, since=since, dry_run=not args.commit)
    except (KeyError, AttributeError, OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"Route Taskboard demo reviews: {type(error).__name__}. Check the operator configuration and provider access.\n")
    print(json.dumps(result, sort_keys=True))
    return int(bool(result.get("invalid")))


if __name__ == "__main__":
    raise SystemExit(main())
