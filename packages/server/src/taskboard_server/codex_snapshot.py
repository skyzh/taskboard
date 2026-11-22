"""Install one approved Codex snapshot outcome from private standard input."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from fcntl import LOCK_EX, flock
import json
import os
import re
from stat import S_IMODE, S_ISREG
import sys
from tempfile import NamedTemporaryFile

import tasks_codex_usage as usage


FAILURES = {"timeout", "unavailable", "provider_error"}
WINDOWS = {"primary", "secondary"}
WINDOW_FIELDS = {"usedPercent": "remaining_percent", "windowDurationMins": "duration_minutes", "resetsAt": "resets_at"}
OPAQUE_LABEL = re.compile(r"[0-9a-fA-F-]{32,36}")


def _time(value):
    if not isinstance(value, str):
        raise ValueError("A UTC-offset timestamp is required.")
    at = datetime.fromisoformat(value)
    if at.tzinfo is None:
        raise ValueError("A UTC-offset timestamp is required.")
    return at.astimezone(timezone.utc)


def _fields(value, allowed):
    if not isinstance(value, dict) or value.keys() - allowed:
        raise ValueError("Unexpected Codex snapshot fields.")
    return value


def _windows(value):
    for window in value.values():
        _fields(window, WINDOW_FIELDS.keys())
        projected = usage._window(window)
        if window and any(projected[WINDOW_FIELDS[field]] is None for field in window):
            raise ValueError("Invalid Codex quota window.")


def _validate_result(snapshot):
    result = _fields(snapshot["result"], {"rateLimits", "rateLimitsByLimitId", "rateLimitResetCredits"})
    overall = _fields(result.get("rateLimits"), WINDOWS | {"credits"})
    _windows({key: overall[key] for key in WINDOWS & overall.keys()})
    credits = _fields(overall.get("credits", {}), {"balance", "unlimited"})
    buckets = result.get("rateLimitsByLimitId", {})
    if not isinstance(buckets, dict) or any(
        not usage._BUCKET.fullmatch(label) or OPAQUE_LABEL.fullmatch(label) for label in buckets
    ):
        raise ValueError("Invalid Codex quota bucket label.")
    for bucket in buckets.values():
        _windows(_fields(bucket, WINDOWS))
    resets = _fields(result.get("rateLimitResetCredits", {}), {"availableCount"})
    summary = usage.summarize(snapshot)
    if (("balance" in credits and summary["paid_balance"] is None)
            or ("unlimited" in credits and type(credits["unlimited"]) is not bool)
            or ("availableCount" in resets and summary["reset_credits"] is None)):
        raise ValueError("Invalid Codex credit data.")


def _object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate Codex snapshot field.")
    return result


def _decode(raw):
    if len(raw) > usage._MAX_BYTES:
        raise ValueError("Codex snapshot is too large.")
    return json.loads(raw, object_pairs_hook=_object)


def _outcome(raw, config):
    envelope = _decode(raw)
    if not isinstance(envelope, dict) or not isinstance(envelope.get("source"), str):
        raise ValueError("A configured source is required.")
    source = envelope["source"]
    paths = dict(config.sources) if config and len(config.sources) == 2 else {}
    if source not in paths:
        raise ValueError("A configured source is required.")
    if len({os.path.realpath(path) for path in paths.values()}) != 2:
        raise ValueError("Configure two distinct snapshot files.")
    at = _time(envelope.get("attempted_at"))
    snapshot = {"attempted_at": at.isoformat()}
    common = {"source", "attempted_at", "status"}
    if envelope.get("status") == "success" and envelope.keys() == common | {"captured_at", "result"}:
        captured = _time(envelope["captured_at"])
        if captured < at:
            raise ValueError("The capture time precedes the read attempt.")
        snapshot.update(captured_at=captured.isoformat(), result=envelope["result"])
        _validate_result(snapshot)
    elif (envelope.get("status") == "failure" and envelope.keys() == common | {"failure"}
          and isinstance(envelope["failure"], str) and envelope["failure"] in FAILURES):
        snapshot.update(status="unavailable", failure=envelope["failure"])
    else:
        raise ValueError("Invalid Codex snapshot outcome.")
    return paths[source], at, snapshot


def _replace(path, snapshot, previous_stat):
    encoded = json.dumps(snapshot, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > usage._MAX_BYTES:
        raise ValueError("Codex snapshot is too large.")
    staging = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=".codex-snapshot-", delete=False) as handle:
            staging = handle.name
            staged = os.fstat(handle.fileno())
            if (staged.st_uid, staged.st_gid) != (previous_stat.st_uid, previous_stat.st_gid):
                os.fchown(handle.fileno(), previous_stat.st_uid, previous_stat.st_gid)
            os.fchmod(handle.fileno(), S_IMODE(previous_stat.st_mode))
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
        staging = None
    finally:
        if staging is not None:
            os.unlink(staging)


@contextmanager
def _locked_directory(path):
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if S_IMODE(os.fstat(directory).st_mode) & 0o077:
            raise ValueError("Use the existing owner-only snapshot directory.")
        flock(directory, LOCK_EX)
        yield directory
    finally:
        os.close(directory)


def install(raw, config):
    path, attempted_at, snapshot = _outcome(raw, config)
    with _locked_directory(path.parent) as directory:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as handle:
            previous_stat = os.fstat(handle.fileno())
            if (not S_ISREG(previous_stat.st_mode) or previous_stat.st_nlink != 1
                    or S_IMODE(previous_stat.st_mode) & 0o077):
                raise ValueError("Use an existing owner-only snapshot file.")
            previous = _decode(handle.read(usage._MAX_BYTES + 1))
        if not isinstance(previous, dict):
            raise ValueError("The existing snapshot has no timestamp.")
        if attempted_at <= _time(previous.get("attempted_at", previous.get("captured_at"))):
            return "skipped"
        if "result" not in snapshot and previous.get("captured_at"):
            snapshot["captured_at"] = _time(previous["captured_at"]).isoformat()
        _replace(path, snapshot, previous_stat)
        return "installed" if "result" in snapshot else "unavailable"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        status = install(sys.stdin.buffer.read(usage._MAX_BYTES + 1), usage.configuration(os.environ))
    except (OSError, ValueError, OverflowError, RecursionError):
        parser.exit(1, "Codex snapshot was not installed. Check the private configuration and outcome.\n")
    print(status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
