"""Validate a typed merge receipt before recording it."""

import json
import re


DETAIL_SCHEMA = "taskboard-merge/v1"


GIT_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


STATUS_AFTER_ACTION = {
    "review": "reviewed",
    "conflict": "conflict",
    "checks_failed": "checks_failed",
    "clear": "reviewed",
    "merged": "merged",
}


def merge_details(details):
    if not details or not details.startswith("{"):
        return None
    try:
        payload = json.loads(details)
    except (ValueError, RecursionError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != DETAIL_SCHEMA:
        return None

    action = payload.get("action")
    if not isinstance(action, str) or (action != "submit" and action not in STATUS_AFTER_ACTION):
        raise ValueError("Choose a supported local merge queue action.")
    fields = {"schema", "action", "candidate", "target"}
    if action == "merged":
        fields.add("squash")
        squash = payload.get("squash")
        if not isinstance(squash, str) or not GIT_COMMIT.fullmatch(squash):
            raise ValueError("A merged receipt needs the full landed Git SHA in the squash field.")
    if payload.keys() != fields:
        raise ValueError("Use the documented local merge queue fields.")
    candidate = payload["candidate"]
    if not isinstance(candidate, str) or not GIT_COMMIT.fullmatch(candidate):
        raise ValueError("Use a full lowercase Git commit ID for candidate.")
    target = payload["target"]
    if not isinstance(target, str) or not 1 <= len(target) <= 160 or not all(
        char.isprintable() and not char.isspace() for char in target
    ):
        raise ValueError("Use a local target branch name without spaces.")
    return payload
