"""Read formal approval receipts from Stamp's structured pull request view."""

from dataclasses import dataclass
import json
import re
import subprocess
from urllib.parse import urlsplit

from tasks_extensions import COMMIT
from tasks_github import _source, repository


RECEIPT = re.compile(r"[0-9a-f]{32}")
SERVER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}")


@dataclass(frozen=True)
class Approval:
    evidence_id: str
    evidence_url: str
    reviewer: str
    server: str
    head: str
    base: str
    ready_to_merge: bool


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
                               reviewer, server["id"], head, base, ready))
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
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError("Stamp could not read the linked pull request.") from error
        try:
            document = json.loads(result.stdout)
        except ValueError as error:
            raise ValueError("Stamp did not return a structured pull request view.") from error
        return approvals(document, item)
