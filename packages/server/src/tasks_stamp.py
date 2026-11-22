"""Read formal approval receipts from Stamp's structured pull request view."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import subprocess
from urllib.parse import urlsplit

import tasks_core as ledger
from tasks_extensions import COMMIT, STAMP_REVIEWS, record_observation
from tasks_github import _source, linked_source, repository
import tasks_peer_asks as peer_asks
import tasks_storage


RECEIPT = re.compile(r"[0-9a-f]{32}")
SERVER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}")


class ReaderUnavailable(RuntimeError):
    """The configured runtime cannot start the Stamp reader."""


def review_health(db, project, repo):
    if repo is None:
        return {"state": "unconfigured"}
    row = db.execute(
        """SELECT pull, checked_at, succeeded_at, failure FROM stamp_review_health
           WHERE project = ? AND repository = ?""", (project, repo),
    ).fetchone()
    if row is None:
        return {"state": "unchecked", "repository": repo}
    return {"state": "failing" if row["failure"] else "verified", "repository": repo, **dict(row)}


def save_health(db, project, repo, url, failure=None):
    at = datetime.now(timezone.utc).isoformat()
    db.execute(
        """INSERT INTO stamp_review_health VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (project, repository) DO UPDATE SET
               pull = excluded.pull, checked_at = excluded.checked_at,
               succeeded_at = COALESCE(excluded.succeeded_at, stamp_review_health.succeeded_at),
               failure = excluded.failure""",
        (project, repo, url, at, None if failure else at, failure),
    )


@dataclass(frozen=True)
class Approval:
    evidence_id: str
    evidence_url: str
    reviewer: str
    server: str
    head: str
    base: str
    ready_to_merge: bool
    merged_with_checks: bool = False


def approvals(document, item):
    if not isinstance(document, dict) or document.get("repository") != repository(item) or document.get("number") != int(item.number):
        raise ValueError("Stamp returned a different pull request.")
    verdict = document.get("verdict")
    head, base = document.get("headSha"), document.get("baseSha")
    if (not isinstance(verdict, dict) or not isinstance(head, str) or not COMMIT.fullmatch(head)
            or not isinstance(base, str) or not COMMIT.fullmatch(base)
            or (verdict.get("headSha"), verdict.get("baseSha")) != (head, base)):
        raise ValueError("Stamp did not verify the pull request revision.")
    url = document.get("stampUrl")
    if not isinstance(url, str):
        raise ValueError("Stamp did not return a pull request link.")
    stamp_url = urlsplit(url)
    path = f"/github/{repository(item)}/pull/{item.number}"
    if (stamp_url.scheme != "https" or stamp_url.netloc != "stamp.build" or stamp_url.path != path
            or stamp_url.query or stamp_url.fragment):
        raise ValueError("Stamp returned an unexpected pull request link.")
    ready = (
        verdict.get("state") == "READY" and verdict.get("requiredKnown") is True
        and document.get("baseRefHeadSha") == base and document.get("mergeBaseSha") == base
        and document.get("stale") is False and document.get("invalidated") is False
    )
    passed = verdict.get("requiredPassed")
    merged_with_checks = (
        verdict.get("state") == "MERGED" and verdict.get("requiredKnown") is True
        and document.get("mergeBaseSha") == base
        and document.get("stale") is False and document.get("invalidated") is False
        and isinstance(passed, list) and bool(passed)
        and all(isinstance(name, str) and name for name in passed)
        and all(verdict.get(key) == [] for key in (
            "requiredFailing", "requiredPending", "requiredSkipped", "requiredSkippedByWorkflow",
        ))
    )
    records = verdict.get("approvals")
    if not isinstance(records, list):
        raise ValueError("Stamp did not return its formal approval records.")
    result = []
    for row in records:
        if not isinstance(row, dict) or row.get("source") != "stamp_receipt" or row.get("headSha") != head:
            continue
        identity = row.get("approvedBy")
        if not isinstance(identity, dict) or identity.get("kind") != "agent":
            continue
        server, reviewer = identity.get("server"), identity.get("id")
        slug, receipt = row.get("approvedByServer"), row.get("approvalId")
        if (not isinstance(server, dict) or not isinstance(server.get("id"), str)
                or not isinstance(reviewer, str) or not reviewer or reviewer != row.get("principal")
                or not isinstance(slug, str) or not SERVER.fullmatch(slug)
                or not isinstance(receipt, str) or not RECEIPT.fullmatch(receipt)):
            continue
        result.append(Approval(receipt, f"https://stamp.build/s/{slug}{path}/approvals/{receipt}/evidence",
                               reviewer, server["id"], head, base, ready,
                               merged_with_checks and row.get("carried") is None))
    return result


class StampReader:
    def __init__(self, command=("stamp",)):
        self.command = command

    def approvals(self, pull):
        item = _source(pull)
        if item.kind != "pull":
            raise ValueError("Stamp reviews require an explicitly linked pull request.")
        try:
            result = subprocess.run(
                [*self.command, "pr", "view", item.number, "--repo", repository(item), "--json"],
                capture_output=True, text=True, timeout=20, check=True,
            )
        except OSError as error:
            raise ReaderUnavailable("The Stamp CLI could not start. Check the operator's installed executable and PATH.") from error
        except subprocess.SubprocessError as error:
            raise RuntimeError("Stamp could not read the linked pull request.") from error
        try:
            document = json.loads(result.stdout)
        except ValueError as error:
            raise ValueError("Stamp did not return a structured pull request view.") from error
        return approvals(document, item)


def linked_tickets(db, url, repo, configured):
    return [row for row in db.execute(
        """SELECT tasks.id, tasks.project_prefix, MIN(sources.id) AS source_id
           FROM sources JOIN task_sources ON task_sources.source_id = sources.id
           JOIN tasks ON tasks.id = task_sources.task_id
           WHERE sources.kind = 'github' AND LOWER(RTRIM(sources.target, '/')) = ?
           GROUP BY tasks.id, tasks.project_prefix ORDER BY tasks.id""", (url,),
    ) if configured.get(row["project_prefix"], {}).get(STAMP_REVIEWS) == repo]


def refresh_approvals(db, reader, configured, pull, *, require_link=False):
    """Record a linked approval and apply it to the exact current peer request."""
    key, url = linked_source(pull)
    item = _source(url)
    if item.kind != "pull":
        raise ValueError("Stamp reviews require an explicitly linked pull request.")
    repo = repository(item)
    result = {"recorded": 0, "ready_for_acknowledgment": 0}
    selected = linked_tickets(db, url, repo, configured)
    if not selected:
        if require_link:
            raise ValueError("Choose a pull request linked to a ticket in this project and its configured repository.")
        return result
    selected_projects = {row["project_prefix"] for row in selected}
    try:
        observations = reader.approvals(url)
    except (RuntimeError, ValueError) as error:
        failure = "reader_unavailable" if isinstance(error, ReaderUnavailable) else "read_failed"
        with tasks_storage.write(db):
            for project in selected_projects:
                save_health(db, project, repo, url, failure)
        if isinstance(error, ReaderUnavailable):
            raise
        raise RuntimeError("Stamp could not verify the linked pull request response.") from None
    with tasks_storage.write(db):
        for project in selected_projects:
            save_health(db, project, repo, url)
        for row in linked_tickets(db, url, repo, configured):
            for approval in observations:
                person = db.execute(
                    """SELECT ledger_key, COALESCE(NULLIF(name, ''), NULLIF(username, ''), ledger_key) AS name
                       FROM board_principals WHERE server_id = ? AND kind = 'agent' AND subject = ?""",
                    (approval.server, approval.reviewer),
                ).fetchone()
                if person is None:
                    continue
                verdict = {"id": approval.evidence_id, "pull": key, "head": approval.head,
                           "base": approval.base, "verdict": "approved", "reviewer": person["ledger_key"]}
                note = f"{person['name']} approved the linked pull request in Stamp."
                recorded = record_observation(
                    db, row["id"], row["source_id"], f"stamp-approval:{approval.evidence_id}",
                    "Stamp", note, [("Stamp approval", approval.evidence_url)],
                    {"extension": STAMP_REVIEWS, "stamp_verdict": verdict},
                )
                result["recorded"] += int(recorded is not None)
                if approval.ready_to_merge or approval.merged_with_checks:
                    task = recorded[0] if recorded is not None else ledger.get_task(db, row["id"])
                    ask = peer_asks.current_wait(db, task)
                    if ask and peer_asks.apply_stamp_verdict(
                        db, ask["task_id"], pull=key, reviewer=person["ledger_key"], verdict="approved",
                        head=approval.head, base=approval.base, evidence_id=approval.evidence_id,
                        evidence_url=approval.evidence_url, current_head=approval.head,
                        current_base=approval.base, checks_passed=True, merged=approval.merged_with_checks,
                    ):
                        result["ready_for_acknowledgment"] += 1
        if any(result.values()):
            ledger.bump_revision(db)
    return result
