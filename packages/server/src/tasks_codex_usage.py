"""Read an operator-supplied Codex quota snapshot for one verified human."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from uuid import UUID


_MAX_BYTES = 128 * 1024
_BUCKET = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,59}")
_BALANCE = re.compile(r"[0-9]{1,18}(?:\.[0-9]{1,8})?")


def _object(value):
    return value if isinstance(value, dict) else {}


def _window(value):
    value = _object(value)
    if not value:
        return None
    used = value.get("usedPercent")
    duration = value.get("windowDurationMins")
    reset = value.get("resetsAt")
    return {
        "remaining_percent": 100 - used if type(used) in (int, float) and 0 <= used <= 100 else None,
        "duration_minutes": duration if type(duration) is int and duration > 0 else None,
        "resets_at": reset if type(reset) is int and 0 <= reset <= 253402300799 else None,
    }


def summarize(snapshot):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("captured_at"), str):
        raise ValueError("A dated Codex usage snapshot is required.")
    at = datetime.fromisoformat(snapshot["captured_at"])
    if at.tzinfo is None:
        raise ValueError("Use a capture time with a UTC offset.")
    result = _object(snapshot.get("result"))
    overall = _object(result.get("rateLimits"))
    if not overall:
        raise ValueError("A Codex rate limit summary is required.")
    buckets = {
        key: value for key, value in _object(result.get("rateLimitsByLimitId")).items()
        if _BUCKET.fullmatch(key) and isinstance(value, dict)
    }
    credits = _object(overall.get("credits"))
    balance = credits.get("balance")
    reset_count = _object(result.get("rateLimitResetCredits")).get("availableCount")
    return {
        "captured_at": at.astimezone(timezone.utc).isoformat(),
        "sample": snapshot.get("sample") is True,
        "buckets": [
            {"label": label, "primary": _window(entry.get("primary")), "secondary": _window(entry.get("secondary"))}
            for label, entry in sorted((buckets or {"Codex": overall}).items())
        ],
        "paid_balance": balance if isinstance(balance, str) and _BALANCE.fullmatch(balance) else None,
        "paid_unlimited": credits.get("unlimited") is True,
        "reset_credits": reset_count if type(reset_count) is int and reset_count >= 0 else None,
    }


@dataclass(frozen=True)
class CodexUsage:
    viewer_subject: str
    snapshot: Path

    def read(self):
        try:
            with self.snapshot.open("rb") as handle:
                raw = handle.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                return None
            return summarize(json.loads(raw))
        except (OSError, OverflowError, ValueError):
            return None


def configuration(environment):
    subject = environment.get("TASKS_CODEX_USAGE_VIEWER_SUBJECT")
    file = environment.get("TASKS_CODEX_USAGE_SNAPSHOT")
    if not (subject or file):
        return None
    if not (subject and file and Path(file).is_absolute()):
        raise ValueError("Configure both the Codex usage viewer UUID and an absolute snapshot path.")
    try:
        return CodexUsage(str(UUID(subject)), Path(file))
    except ValueError:
        raise ValueError("Use the verified Raft human subject UUID for the Codex usage viewer.") from None
