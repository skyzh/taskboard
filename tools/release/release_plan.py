"""Artifact checks and a disposable release transaction shared by runner designs."""

from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import fcntl
import json
import os
import re
import shutil
import sqlite3
from tempfile import NamedTemporaryFile, TemporaryDirectory, mkstemp


class ReleaseRejected(ValueError):
    """The queue entry could not be applied to the selected target."""

    def __init__(self, message, code="invalid_entry"):
        super().__init__(message)
        self.code = code


class MountedCheckFailed(RuntimeError):
    """Carry the verified smoke check codes to the private operator receipt."""

    def __init__(self, checks):
        super().__init__("The candidate did not pass mounted health checks.")
        self.checks = checks


def failed_check(error):
    failure = {"failure": type(error).__name__}
    if isinstance(error, MountedCheckFailed):
        failure["failed_checks"] = error.checks
    return failure


def file_hash(path):
    with path.open("rb") as source:
        return sha256(source.read()).hexdigest()


def write_json(path, value):
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        json.dump(value, output, indent=2)
        output.write("\n")
    os.replace(temporary, path)


def verify_manifest(directory, expected):
    directory = Path(directory).resolve(strict=True)
    manifest = directory / "SHA256SUMS"
    if manifest.is_symlink() or file_hash(manifest) != expected:
        raise ReleaseRejected("The artifact manifest does not match the reviewed digest.")
    files = set()
    for line in manifest.read_text(encoding="utf-8").splitlines():
        checksum, separator, name = line.partition("  ")
        relative = Path(name)
        if (
            not separator or not re.fullmatch("[0-9a-f]{64}", checksum)
            or relative.is_absolute() or ".." in relative.parts or not relative.parts
            or relative in files
        ):
            raise ReleaseRejected("The artifact manifest contains an invalid file entry.")
        source = directory / relative
        if source.resolve() != source or not source.is_file() or file_hash(source) != checksum:
            raise ReleaseRejected(f"Artifact verification failed for {relative}.")
        files.add(relative)
    if not files:
        raise ReleaseRejected("The artifact manifest lists no files.")
    return sorted(files)


def check_receipts(entry, *, require_review=True):
    for field in ("ticket", "author") if require_review else ("ticket",):
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ReleaseRejected(f"The release entry needs a {field}.")
    for field in ("candidate", "base"):
        value = entry.get(field)
        if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
            raise ReleaseRejected("Use full SHA-256 digests for the candidate and its base.")
    digest = entry["candidate"]
    checks = entry.get("checks")
    if not isinstance(checks, list) or not checks or any(
        not isinstance(receipt, dict) or receipt.get("candidate") != digest or receipt.get("outcome") != "pass"
        for receipt in checks
    ):
        raise ReleaseRejected("The candidate needs passing check receipts for its exact digest.")
    if not require_review:
        return
    review = entry.get("review")
    reviewer = review.get("reviewer") if isinstance(review, dict) else None
    if (
        not isinstance(reviewer, str) or not reviewer.strip()
        or reviewer == entry["author"] or review.get("candidate") != digest or review.get("outcome") != "pass"
    ):
        raise ReleaseRejected("The candidate needs a passing independent review on its exact digest.")


def copy_release(artifact, stage, digest):
    files = verify_manifest(artifact, digest)
    for relative in [Path("SHA256SUMS"), *files]:
        destination = stage / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(artifact / relative, destination)
    verify_manifest(stage, digest)


def switch_current(current, release):
    with TemporaryDirectory(prefix=".release-link-", dir=current.parent) as temporary:
        candidate = Path(temporary) / "current"
        candidate.symlink_to(release, target_is_directory=True)
        os.replace(candidate, current)


def save_ledger_backup(database, backup):
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(backup)) as destination:
            source.backup(destination)


@dataclass(frozen=True)
class ReleaseLayout:
    current: Path
    releases: Path
    database: Path
    backups: Path
    lock: Path


def run_fixture_release(root, artifact, entry, restart, healthy, journal=None):
    """Replay approved bytes on a disposable target initialized by the test harness."""
    root = Path(root).resolve(strict=True)
    artifact = Path(artifact)
    if not (root / ".tasks-disposable-release-target").is_file():
        raise ReleaseRejected("Select an initialized disposable release target.")
    layout = ReleaseLayout(root / "current", root / "releases", root / "shared" / "tasks.sqlite3",
                           root / "backups", root / ".release-lock")
    return run_release(layout, artifact, entry, restart, healthy, journal)


def run_release(layout, artifact, entry, restart, healthy, journal=None, *, require_review=True):
    with layout.lock.open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ReleaseRejected("Another release is already running on the fixture.") from error
        return release_locked(layout, artifact, entry, restart, healthy, journal, require_review=require_review)


def rollback(layout, previous, base, restart, healthy):
    switch_current(layout.current, previous)
    try:
        restart()
        if not healthy(base):
            raise RuntimeError("The restored release did not pass mounted health checks.")
    except Exception as error:
        return False, failed_check(error)
    return True, None


def restart_candidate(restart, journal, marker):
    restart()
    if journal is not None:
        marker["restarted"] = True
        write_json(journal, marker)


def recover_activation(layout, entry, journal, active, restart, healthy):
    marker = json.loads(journal.read_text(encoding="utf-8"))
    digest, base = entry["candidate"], entry["base"]
    previous_name = marker["previous"]
    if (
        marker["candidate"] != digest or marker["base"] != base
        or Path(previous_name).name != previous_name
    ):
        raise ValueError("Reconcile the existing activation journal with its queued job.")
    previous = (layout.releases / previous_name).resolve(strict=True)
    if previous.parent != layout.releases or active not in (previous, layout.releases / digest):
        raise ValueError("Reconcile the existing activation journal with the active release.")
    try:
        verify_manifest(previous, base)
    except ReleaseRejected as error:
        raise ValueError("Verify the recorded prior release before recovering the job.") from error
    result = {"ticket": entry["ticket"], "candidate": digest, "backup": marker["backup"],
              **marker["execution"]}
    if active == previous and base != digest:
        restart()
        if not healthy(base):
            raise ValueError("Verify the prior release before reconciling an interrupted activation.")
        return {**result, "status": "rejected", "active": base, "reason_code": "activation_interrupted"}
    try:
        verify_manifest(active, digest)
        if not marker["restarted"]:
            restart_candidate(restart, journal, marker)
        if not healthy(digest):
            raise RuntimeError("The candidate did not pass health checks.")
    except Exception as error:
        restored, rollback_failure = rollback(layout, previous, base, restart, healthy)
        return {**result, "status": "rolled_back" if restored else "rollback_failed",
                "active": base if restored else None, "reason_code": "activation_interrupted",
                **failed_check(error), "rollback_failure": rollback_failure}
    return {**result, "status": "released", "active": digest}


def release_locked(layout, artifact, entry, restart, healthy, journal, *, require_review=True):
    releases = layout.releases
    current = layout.current
    if journal is not None and journal.exists():
        if not current.is_symlink():
            raise ValueError("Verify the active release pointer before recovering the job.")
        return recover_activation(layout, entry, journal, current.resolve(strict=True), restart, healthy)
    check_receipts(entry, require_review=require_review)
    digest = entry["candidate"]
    if not current.is_symlink():
        raise ReleaseRejected("The target needs an active release pointer.")
    previous = current.resolve(strict=True)
    if previous.parent != releases:
        raise ReleaseRejected("The active release is outside this fixture's release directory.")
    if file_hash(previous / "SHA256SUMS") != entry["base"]:
        raise ReleaseRejected("The active base advanced. Recheck the candidate on the current release.", "stale_base")
    verify_manifest(previous, entry["base"])

    directory = releases / digest
    if directory.exists():
        verify_manifest(directory, digest)
    else:
        with TemporaryDirectory(prefix=".stage-", dir=releases) as temporary:
            stage = Path(temporary) / "candidate"
            stage.mkdir()
            copy_release(artifact, stage, digest)
            stage.rename(directory)
    descriptor, location = mkstemp(prefix=digest + "-", suffix=".sqlite3", dir=layout.backups)
    os.close(descriptor)
    backup = Path(location)
    save_ledger_backup(layout.database, backup)
    marker = None
    if journal is not None:
        marker = {"candidate": digest, "base": entry["base"], "previous": previous.name,
                  "backup": backup.name, "execution": entry["execution"], "restarted": False}
        write_json(journal, marker)
    switch_current(current, directory)

    try:
        restart_candidate(restart, journal, marker)
        if not healthy(digest):
            raise RuntimeError("The candidate did not pass health checks.")
    except Exception as failure:
        restored, rollback_failure = rollback(layout, previous, entry["base"], restart, healthy)
        return {
            "ticket": entry["ticket"],
            "candidate": digest,
            "active": entry["base"] if restored else None,
            "status": "rolled_back" if restored else "rollback_failed",
            **failed_check(failure),
            "rollback_failure": rollback_failure,
            "backup": backup.name,
        }

    return {
        "ticket": entry["ticket"],
        "candidate": digest,
        "active": digest,
        "status": "released",
        "backup": backup.name,
    }
