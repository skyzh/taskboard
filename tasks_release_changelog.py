"""Validate release notes shared by operator admission and ledger import."""

import re


GIT_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
TICKET = re.compile(r"T-([1-9][0-9]{0,18})")
MAX_BOARD_ID = 2**63 - 1


def normalize_changelog(data):
    if not isinstance(data, dict) or data.keys() != {"base_commit", "head_commit", "entries"}:
        raise ValueError("Provide the release changelog as an exact Git range and a list of entries.")
    base, head = data["base_commit"], data["head_commit"]
    if base == head or not all(isinstance(commit, str) and GIT_COMMIT.fullmatch(commit)
                               for commit in (base, head)):
        raise ValueError("Provide different full Git commit IDs for the changelog base and head.")
    if not isinstance(data["entries"], list) or not data["entries"]:
        raise ValueError("Record at least one change for this release.")

    entries = []
    receipt_ids = set()
    ticket_merges = set()
    for entry in data["entries"]:
        if not isinstance(entry, dict) or entry.keys() != {"ticket", "summary", "merge_commit", "merge_receipt_id"}:
            raise ValueError("Record each change with its ticket, summary, merge commit and Activity receipt ID.")
        ticket, summary, commit, receipt_id = (
            entry[key] for key in ("ticket", "summary", "merge_commit", "merge_receipt_id")
        )
        match = TICKET.fullmatch(ticket) if isinstance(ticket, str) else None
        if match is None or int(match[1]) > MAX_BOARD_ID:
            raise ValueError("Changelog entries require a Taskboard ticket such as T-169.")
        if not isinstance(summary, str):
            raise ValueError("Write each changelog summary on one line, up to 160 characters.")
        summary = summary.strip()
        if len(summary) > 160 or len(summary.splitlines()) != 1:
            raise ValueError("Write each changelog summary on one line, up to 160 characters.")
        if not isinstance(commit, str) or not GIT_COMMIT.fullmatch(commit):
            raise ValueError("Use the full merged Git commit ID in each changelog entry.")
        if (ticket, commit) in ticket_merges:
            raise ValueError("List each merged commit once per changelog ticket.")
        if type(receipt_id) is not int or not 0 < receipt_id < MAX_BOARD_ID or receipt_id in receipt_ids:
            raise ValueError("Use a distinct positive Activity receipt ID for each changelog entry.")
        ticket_merges.add((ticket, commit))
        receipt_ids.add(receipt_id)
        entries.append({**entry, "summary": summary})
    return {"base_commit": base, "head_commit": head, "entries": entries}
