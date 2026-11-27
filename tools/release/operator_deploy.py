"""Apply approved Taskboard app jobs through an already authorized service owner."""

import argparse
from datetime import datetime, timezone
from functools import partial
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import tarfile
import time

from local_queue import checked_results, drain_queue, elapsed_ms, fixture_verifier, queue_lock, write_json
from publish_receipt import publish_outcome, release_event, signed_in_service
from queue_provenance import GIT_COMMIT, ticket_number
from release_plan import (
    MountedCheckFailed, ReleaseLayout, ReleaseRejected, file_hash, release_lock, run_release, verify_manifest,
)
from release_slots import ReleaseSlots
from stage_transfer import extract_transfer


SERVICE_NAME = re.compile(r"[A-Za-z0-9_.@-]+\.service")
RETRY_ATTEMPT = re.compile(r"[0-9a-f]{32}")
TEMPORARY_HEALTH_FAILURES = {"request_failed", "unexpected_http_502", "unexpected_http_503", "unexpected_http_504"}
STARTUP_ATTEMPTS = 8


def deployment_layout(data):
    current = Path(data["current"]).absolute()
    return ReleaseLayout(
        current,
        Path(data["releases"]).resolve(strict=True),
        Path(data["ledger"]).resolve(strict=True),
        Path(data["backups"]).resolve(strict=True),
        Path(data["lock"]).absolute(),
    )


def service_restart(unit, run=subprocess.run):
    if not isinstance(unit, str) or not SERVICE_NAME.fullmatch(unit) or unit.startswith("-"):
        raise ValueError("Set the existing Taskboard user service unit.")

    def restart():
        run(["systemctl", "--user", "restart", unit], check=True, timeout=45)

    return restart


def mounted_verifier(layout, package, package_manifest, python, mount, avatars,
                     run=subprocess.run, pause=time.sleep):
    package = Path(package).resolve(strict=True)
    if not avatars:
        raise ValueError("Configure an expected mounted avatar for the release smoke check.")

    def healthy(expected):
        verify_manifest(package, package_manifest)
        command = [python, "-B", package / "release_smoke.py", "--mount-url", mount,
                   "--manifest", layout.current / "SHA256SUMS", "--manifest-sha256", expected]
        for avatar in avatars:
            command.extend(["--avatar", avatar])
        for attempt in range(STARTUP_ATTEMPTS):
            try:
                result = run(command, capture_output=True, text=True, timeout=75)
            except subprocess.TimeoutExpired as error:
                raise MountedCheckFailed([{"name": "smoke", "reason": "timeout"}]) from error
            try:
                receipt = json.loads(result.stdout)
            except (TypeError, ValueError) as error:
                raise MountedCheckFailed([{"name": "smoke", "reason": "invalid_receipt"}]) from error
            if not isinstance(receipt, dict) or receipt.get("schema_version") != 1:
                raise MountedCheckFailed([{"name": "smoke", "reason": "invalid_receipt"}])
            if not result.returncode and receipt.get("status") == "pass":
                if receipt.get("manifest_sha256") == expected:
                    return True
                raise MountedCheckFailed([{"name": "smoke", "reason": "manifest_sha256_mismatch"}])
            checks = receipt.get("checks")
            failures = [
                {"name": check["name"], "reason": check["reason"]}
                for check in checks if isinstance(check, dict) and check.get("status") == "fail"
                and isinstance(check.get("name"), str) and isinstance(check.get("reason"), str)
            ] if isinstance(checks, list) else []
            starting = any(check["name"] == "health" and check["reason"] in TEMPORARY_HEALTH_FAILURES
                           for check in failures)
            if not starting or attempt == STARTUP_ATTEMPTS - 1:
                raise MountedCheckFailed(failures or [{"name": "smoke", "reason": "failed_without_details"}])
            pause(1)

    return healthy


def apply_app(layout, artifact, job, restart, healthy, journal=None, *, slots=None):
    if job.get("portal"):
        raise ReleaseRejected("Use the existing coordinated app and portal release lane.", "portal_scope")
    return run_release(layout, artifact, job, restart, healthy, journal, slots=slots)


def checked_main_receipt(config, commit, attempt=None):
    if GIT_COMMIT.fullmatch(commit) is None:
        raise ValueError("Provide the full main commit.")
    if attempt is not None and RETRY_ATTEMPT.fullmatch(attempt) is None:
        raise ValueError("Use a 32-character lowercase hexadecimal retry attempt ID.")
    suffix = f"-retry-{attempt}" if attempt is not None else ""
    return Path(config["queue"]) / "results" / f"release-main-{commit}{suffix}.json"


def retry_origin(config, commit):
    receipt = checked_main_receipt(config, commit)
    if not receipt.is_file():
        raise ReleaseRejected("The checked commit has no original rejected outcome to retry.")
    outcome = json.loads(receipt.read_text(encoding="utf-8"))
    if outcome.get("commit") != commit or outcome.get("status") != "rejected":
        raise ReleaseRejected("A checked retry requires the original rejected outcome for that commit.")
    return outcome


def require_reconciled_retries(original_receipt, current_receipt=None):
    for journal in original_receipt.parent.glob(f"{original_receipt.stem}-retry-*.activation"):
        result = journal.with_suffix(".json")
        if result != current_receipt and not result.is_file():
            raise ReleaseRejected(
                f"Reconcile the interrupted Pi activation journal {journal.name} before retrying.",
                "activation_unreconciled",
            )


def read_main_state(config, layout, commit, *, attempt=None):
    receipt = checked_main_receipt(config, commit, attempt)
    queue = receipt.parent.parent
    queue.mkdir(mode=0o700, parents=True, exist_ok=True)
    with queue_lock(queue):
        original = retry_origin(config, commit) if attempt is not None else None
        if receipt.is_file():
            outcome = json.loads(receipt.read_text(encoding="utf-8"))
        elif receipt.with_suffix(".activation").is_file():
            raise ReleaseRejected("An activation was interrupted before saving its outcome. Reconcile the Pi journal.")
        else:
            if attempt is not None:
                require_reconciled_retries(checked_main_receipt(config, commit))
            outcome = None
        return {"outcome": outcome, "active": file_hash(layout.current / "SHA256SUMS"),
                **({"original": original} if original is not None else {})}


def receive_checked_main(config, layout, commit, candidate, base, source, verify, restart, healthy, *,
                         slots=None, attempt=None, preflight=None):
    if any(re.fullmatch("[0-9a-f]{64}", digest) is None for digest in (candidate, base)):
        raise ValueError("Provide full candidate and active-base digests.")
    if attempt is not None and preflight is None:
        raise ValueError("Use the checked hosted runner to authorize a retry.")
    receipt = checked_main_receipt(config, commit, attempt)
    queue = receipt.parent.parent
    ticket = config.get("queue_ticket", "T-78")
    ticket_number(ticket)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    journal = receipt.with_suffix(".activation")
    with TemporaryDirectory(prefix=".transfer-", dir=queue) as staging:
        artifact = Path(staging)
        extract_transfer(source, artifact, candidate)
        with queue_lock(queue):
            original = retry_origin(config, commit) if attempt is not None else None
            if original is not None and (original["candidate"], original["base"]) != (candidate, base):
                raise ReleaseRejected("A checked retry requires the original artifact and active base.")
            if receipt.is_file():
                result = json.loads(receipt.read_text(encoding="utf-8"))
                if (result["commit"], result["candidate"], result["base"]) != (commit, candidate, base):
                    raise ReleaseRejected("The stored result belongs to different release inputs.")
                return result
            if attempt is not None:
                require_reconciled_retries(checked_main_receipt(config, commit), current_receipt=receipt)
            started = datetime.now(timezone.utc)
            stage = "fixtures"
            try:
                job = {"ticket": ticket, "candidate": candidate, "base": base}
                reports = (json.loads(journal.read_text(encoding="utf-8"))["execution"]["checks"]
                           if journal.is_file() else verify(artifact, job))
                job["checks"] = checked_results(job, reports)
                job["execution"] = {"checks": reports, "started_at": started.isoformat()}
                stage = "deployment"
                result = run_release(layout, artifact, job, restart, healthy, journal,
                                     require_review=False, slots=slots, preflight=preflight)
            except ReleaseRejected as error:
                result = {"ticket": ticket, "candidate": candidate, "status": "rejected",
                          "reason_code": stage if error.code == "invalid_entry" else error.code,
                          "reason": str(error)}
            finished = datetime.now(timezone.utc)
            result.update(release_id=receipt.stem, commit=commit, base=base,
                          at=finished.isoformat(timespec="milliseconds"),
                          deploy_ms=elapsed_ms(started.isoformat(), finished))
            if original is not None:
                result["retry_of"] = original["release_id"]
            write_json(receipt, result)
            return result


def project_checked_main(config, layout, commit, activity_url, python, run, *, attempt=None):
    mount = config["mount"].rstrip("/")
    if not re.fullmatch(re.escape(mount) + r"/#event-[1-9][0-9]*-log", activity_url):
        raise ValueError("Use the exact Activity permalink on the configured board.")
    receipt = checked_main_receipt(config, commit, attempt)
    result = json.loads(receipt.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix=".projection-", dir=receipt.parent) as directory:
        projection = Path(directory) / "release.json"
        write_json(projection, release_event(result, receipt_url=activity_url))
        launched = run([python, "-B", layout.current / "tasks", "--offline", "--db", layout.database,
                        "release-event", projection], capture_output=True, text=True, check=True, timeout=30)
    return json.loads(launched.stdout)


def run_config(config, record, run=subprocess.run, *, checked_main=None, candidate=None, base=None,
               activity_url=None, read_state=False, source=None, restore_selected_slot=False,
               attempt=None, preflight=None):
    if type(config.get("operator_uid")) is not int or config["operator_uid"] != os.getuid():
        raise ValueError("Run under the existing authorized Taskboard service account.")
    layout = deployment_layout(config)
    python = config.get("python", sys.executable)
    if restore_selected_slot:
        with release_lock(layout):
            ReleaseSlots(config["slots"], layout.database, run=run).restore_selected(layout)
        return None
    if read_state:
        return read_main_state(config, layout, checked_main, attempt=attempt)
    if activity_url is not None:
        return project_checked_main(config, layout, checked_main, activity_url, python, run, attempt=attempt)
    package, pin = config["checks_package"], config["checks_manifest"]
    verify = fixture_verifier(package, pin, python, config["suites"])
    restart = service_restart(config["unit"], run)
    healthy = mounted_verifier(layout, package, pin, python, config["mount"], config.get("avatars", ()), run)
    slots = ReleaseSlots(config["slots"], layout.database, run=run) if "slots" in config else None
    if checked_main is not None:
        return receive_checked_main(config, layout, checked_main, candidate, base, source, verify, restart, healthy,
                                    slots=slots, attempt=attempt, preflight=preflight)
    return drain_queue(config["queue"], layout, config["artifacts"], verify, restart, healthy, record,
                       partial(apply_app, slots=slots))


def read_operator_config(path):
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        raise ValueError("The operator configuration must have an authorized owner and private write access.")
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Private existing-operator configuration.")
    parser.add_argument("--record-service", help="Record outcomes through this already signed-in Tasks service.")
    parser.add_argument("--checked-main", help="Receive the exact checked main transfer from the build host.")
    parser.add_argument("--candidate", help="Exact transfer manifest SHA-256.")
    parser.add_argument("--base", help="Exact active manifest SHA-256 used for compatibility.")
    parser.add_argument("--attempt-id", help="Select a retry receipt for readback or projection.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--activity-url", help="Project the saved outcome linked to its exact Activity event.")
    action.add_argument("--read-state", action="store_true", help="Read the Pi manifest and any saved outcome.")
    action.add_argument("--restore-selected-slot", action="store_true", help="Start the configured pinned slot at boot.")
    args = parser.parse_args()
    if args.checked_main is None and any((args.candidate, args.base, args.activity_url, args.read_state)):
        parser.error("The selected operation requires --checked-main.")
    if args.attempt_id is not None and not (args.checked_main and (args.activity_url or args.read_state)):
        parser.error("Use --attempt-id here only to read or project an existing checked retry.")
    if args.checked_main and args.record_service:
        parser.error("The checked-main build host publishes its outcome.")
    if args.restore_selected_slot and (args.checked_main or args.record_service):
        parser.error("Restore the selected slot as a standalone operator action.")
    if args.checked_main and not (args.activity_url or args.read_state) and not (args.candidate and args.base):
        parser.error("The checked transfer needs --candidate and --base.")
    if (args.activity_url or args.read_state) and (args.candidate or args.base):
        parser.error("Read or project a saved outcome without a new transfer.")
    try:
        config = read_operator_config(args.config)
        invoke = signed_in_service(args.record_service) if args.record_service else None

        def record(outcome):
            if invoke is not None:
                publish_outcome(invoke, outcome)
            print(json.dumps(outcome), flush=True)

        outcome = run_config(config, record, checked_main=args.checked_main, candidate=args.candidate,
                             base=args.base, activity_url=args.activity_url, read_state=args.read_state,
                             source=sys.stdin.buffer, restore_selected_slot=args.restore_selected_slot,
                             attempt=args.attempt_id)
        if args.checked_main:
            print(json.dumps(outcome), flush=True)
    except (OSError, KeyError, ValueError, MountedCheckFailed, subprocess.SubprocessError, tarfile.TarError) as error:
        parser.exit(1, f"Release operator: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
