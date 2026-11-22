"""Drain operator-prepared Tasks release jobs against the disposable lane."""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import fcntl
import json
import re
import subprocess

from release_plan import ReleaseRejected, run_fixture_release, verify_manifest, write_json


def elapsed_ms(start, end):
    if not start:
        return None
    return round((end - datetime.fromisoformat(start)).total_seconds() * 1000)


@contextmanager
def queue_lock(directory):
    with (directory / ".queue-lock").open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ReleaseRejected("Another runner is draining the release queue.") from error
        yield


def checked_results(job, reports):
    """Accept passing release check reports for the app, and the exact portal when used."""
    if not isinstance(reports, list) or not reports:
        raise ReleaseRejected("Release checks returned no receipts.")
    candidate = job["candidate"]
    portal = job.get("portal")
    portal_checked = False
    checks = []
    for report in reports:
        if not isinstance(report, dict):
            raise ReleaseRejected("Release checks returned an invalid receipt.")
        app_manifest = report.get("app_sha256", report.get("manifest_sha256"))
        if (
            report.get("schema_version") != 1 or report.get("status") != "pass"
            or app_manifest != candidate or not isinstance(report.get("checks"), list)
            or not report["checks"] or not all(
                isinstance(item, dict) and item.get("status") == "pass" for item in report["checks"]
            )
        ):
            raise ReleaseRejected("A release check failed or did not match the exact app.", "check_failed")
        if report.get("check") == "refresh_reconnect":
            if not portal or report.get("portal_sha256") != portal:
                raise ReleaseRejected("The refresh check did not match the exact portal.", "check_failed")
            portal_checked = True
        checks.append({"candidate": candidate, "outcome": "pass"})
    if portal and not portal_checked:
        raise ReleaseRejected("A release with a portal digest needs an exact refresh check.", "check_failed")
    return checks


def fixture_verifier(package, package_manifest, interpreter, suites):
    """Build a verifier from the operator-pinned check package."""
    package = Path(package).resolve(strict=True)
    verify_manifest(package, package_manifest)

    def verify(artifact, job):
        verify_manifest(package, package_manifest)
        command = [
            interpreter, "-B", package / "fixture_checks.py",
            "--artifact", artifact, "--manifest-sha256", job["candidate"],
            "--python", interpreter,
        ]
        for suite in suites:
            command.extend(["--suite", suite])
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=120)
            return [json.loads(result.stdout)]
        except (subprocess.SubprocessError, OSError, ValueError) as error:
            raise ReleaseRejected("The operator-configured fixture check failed.", "check_failed") from error

    return verify


def drain_queue(queue, target, artifacts, verify, restart, healthy, record, apply=run_fixture_release):
    """Consume the trusted local spool and record outcomes through its Tasks adapter.

    Fixture callers own the spool and artifact roots. They supply the approved
    verifier, service controls, and activity recorder. Jobs contain no executable
    commands. This adapter does not create or use a production deployment grant.
    """
    queue = Path(queue)
    artifacts = Path(artifacts)
    results = queue / "results"
    results.mkdir(exist_ok=True)
    with queue_lock(queue):
        jobs = []
        for path in sorted(queue.glob("*.json")):
            job = json.loads(path.read_text(encoding="utf-8"))
            jobs.append((job.get("urgent") is True, path, job))
        jobs.sort(key=lambda entry: not entry[0])
        outcomes = []
        for _, path, job in jobs:
            receipt_path = results / path.name
            delivered = receipt_path.with_suffix(".recorded")
            journal = receipt_path.with_suffix(".activation")
            if delivered.exists():
                continue
            if receipt_path.exists():
                outcome = json.loads(receipt_path.read_text(encoding="utf-8"))
            else:
                started = datetime.now(timezone.utc)
                try:
                    candidate = job.get("candidate")
                    if not isinstance(candidate, str) or not re.fullmatch("[0-9a-f]{64}", candidate):
                        raise ReleaseRejected("The local queue job needs a full candidate digest.")
                    artifact = artifacts / candidate
                    if journal.exists():
                        outcome = apply(target, artifact, job, restart, healthy, journal)
                        started = datetime.fromisoformat(outcome.pop("started_at"))
                    else:
                        try:
                            verify_manifest(artifact, candidate)
                        except OSError as error:
                            raise ReleaseRejected("The queued artifact is unavailable.", "check_failed") from error
                        reports = verify(artifact, job)
                        job["checks"] = checked_results(job, reports)
                        job["execution"] = {"checks": reports, "started_at": started.isoformat()}
                        outcome = apply(target, artifact, job, restart, healthy, journal)
                        outcome["checks"] = reports
                except ReleaseRejected as failure:
                    outcome = {"ticket": job.get("ticket"), "candidate": job.get("candidate"),
                               "status": "rejected", "reason": str(failure), "reason_code": failure.code}
                outcome["release_id"] = path.stem
                finished = datetime.now(timezone.utc)
                outcome["at"] = finished.isoformat(timespec="milliseconds")
                outcome["elapsed_ms"] = round((finished - started).total_seconds() * 1000)
                outcome["queue_ms"] = elapsed_ms(job.get("queued_at"), started)
                outcome["review_to_result_ms"] = elapsed_ms(job.get("reviewed_at"), finished)
                if outcome["status"] == "released" and "changelog" in job:
                    outcome["changelog"] = job["changelog"]
                write_json(receipt_path, outcome)
            record(outcome)
            delivered.touch()
            outcomes.append(outcome)
    return outcomes
