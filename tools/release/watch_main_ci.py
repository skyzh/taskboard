"""Route completed Taskboard main CI failures using the Mac's existing signed sessions."""

import argparse
from datetime import date
import json
import re
from pathlib import Path
import subprocess

from github_main import GitHubRateLimited, protected_tip, read_github, read_github_pages
from local_queue import queue_lock, write_json
from publish_receipt import signed_in_service
from queue_provenance import GIT_COMMIT

REPO = "skyzh/taskboard"
API = f"/repos/{REPO}"
ACTIONS = f"https://github.com/{REPO}/actions/runs"
WORKFLOW = 426408905
PROJECT = "T"


def github_read(path, *, paginate=False):
    command = ["gh", "api", path]
    if paginate:
        command.extend(["--paginate", "--slurp"])
    result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise ValueError("GitHub CLI did not return a successful response.")
    return json.loads(result.stdout)


def public_github_read(path, *, paginate=False):
    if not paginate:
        return read_github(path)
    field = "jobs" if "/jobs?" in path else "workflow_runs"
    return read_github_pages(path, field=field)


def all_items(pages, field):
    items = [item for page in pages for item in page[field]]
    if (not pages or any(page["total_count"] != len(items) for page in pages)
            or len({item["id"] for item in items}) != len(items)):
        raise ValueError("GitHub did not return the full result set.")
    return items


def main_run(run):
    return (run.get("workflow_id") == WORKFLOW
            and run.get("event") == "push" and run.get("head_branch") == "main"
            and (run.get("head_repository") or {}).get("full_name") == REPO)


def number(value):
    if type(value) is not int or value < 1:
        raise ValueError("GitHub returned an invalid run, attempt, or job number.")
    return value


def observations(read, since, seen, pending):
    protected_tip({"repo": REPO}, read)
    pages = read(
        f"{API}/actions/workflows/{WORKFLOW}/runs?branch=main&event=push&created=%3E%3D{since}&per_page=100",
        paginate=True,
    )
    runs = all_items(pages, "workflow_runs")
    listed = {number(run["id"]) for run in runs}
    carried = [read(f"{API}/actions/runs/{number(run)}") for run in sorted(pending - listed)]
    if {run["id"] for run in carried} != pending - listed or not all(main_run(run) for run in carried):
        raise ValueError("GitHub did not confirm the outstanding CI runs.")
    for latest in [*carried, *reversed(runs)]:
        if not main_run(latest):
            continue
        run_id = number(latest["id"])
        commit = latest["head_sha"]
        if not isinstance(commit, str) or not GIT_COMMIT.fullmatch(commit):
            raise ValueError("GitHub did not return a full main commit.")
        last_attempt = number(latest["run_attempt"])
        for attempt in range(seen.get(str(run_id), 0) + 1, last_attempt + 1):
            endpoint = f"{API}/actions/runs/{run_id}/attempts/{attempt}"
            run = latest if attempt == last_attempt else read(endpoint)
            if not main_run(run) or (run["id"], run["run_attempt"], run["head_sha"]) != (run_id, attempt, commit):
                raise ValueError("GitHub did not confirm the exact main run attempt.")
            if run["status"] != "completed":
                yield run_id, attempt, commit, "incomplete", []
                break
            failed = []
            if run["conclusion"] == "failure":
                jobs = all_items(read(endpoint + "/jobs?per_page=100", paginate=True), "jobs")
                failed = [
                    (job["name"], f"{ACTIONS}/{run_id}/job/{number(job['id'])}")
                    for job in jobs if (job["head_sha"] == commit and job["status"] == "completed"
                                        and job["conclusion"] in ("failure", "timed_out"))
                ]
                if not failed:
                    yield run_id, attempt, commit, "missing_failed_jobs", []
                    break
            yield run_id, attempt, commit, run["conclusion"], failed


def activity_matches(invoke, marker, owner, run, *, attempt=None):
    response = invoke("search-activity", {"project": PROJECT, "q": marker, "limit": 100})
    if response["next_cursor"] is not None:
        raise ValueError("The CI source key has too many matching Activity entries.")
    run_url = f"{ACTIONS}/{run}/attempts/"
    url_pattern = re.escape(run_url) + (str(attempt) if attempt else r"[1-9][0-9]*")
    matches = set()
    for candidate in response["items"]:
        key = candidate["task_key"]
        events = invoke("task-activity", {"id": key, "before": candidate["id"] + 1, "limit": 1})["items"]
        if events:
            event = events[0]
            if (event["id"] == candidate["id"] and event["actor"] == owner
                    and event["action"] in (("add", "log") if attempt else ("add",))
                    and event["note"].startswith(marker + " ")
                    and any(link["label"] == "GitHub Actions attempt" and re.fullmatch(url_pattern, link["url"])
                            for link in event["links"])):
                matches.add(key)
    return matches


def reconcile(invoke, event, *, owner, dry_run, planned):
    run, attempt, commit, outcome, failed = event
    if outcome == "missing_failed_jobs":
        return {"run": run, "attempt": attempt, "action": "deferred", "reason": outcome}
    if outcome not in ("failure", "success") or (outcome == "success" and attempt == 1):
        return None
    source = f"Taskboard CI run {run}."
    marker = f"{source} Attempt {attempt}."
    targets = activity_matches(invoke, source, owner, run)
    if len(targets) > 1:
        raise ValueError(f"Multiple tickets match Taskboard CI run {run}.")
    target = next(iter(targets), None)
    if not target and run not in planned and outcome == "success":
        return None
    if target and target in activity_matches(invoke, marker, owner, run, attempt=attempt):
        return {"run": run, "attempt": attempt, "action": "already-recorded", "ticket": target}

    run_url = f"{ACTIONS}/{run}/attempts/{attempt}"
    job_links = "\n".join(f"- {name}: {url}" for name, url in failed)
    evidence = {"label": "GitHub Actions attempt", "url": run_url}
    if target or run in planned:
        action, payload = "record-activity", {
            "id": target, "note": f"{marker} {'Passed' if outcome == 'success' else 'Failed'} on main.",
            "details": f"Commit `{commit}`." + (f"\n\n{job_links}" if job_links else ""),
            **evidence,
        }
    else:
        action, payload = "create-task", {
            "project": PROJECT, "title": f"Investigate failed Taskboard main CI run {run}",
            "step": f"{marker} Open the failed job and identify the first failing check.",
            "brief": f"Taskboard CI failed on protected main at `{commit}`.\n\n{job_links}",
            **evidence,
        }
    result = {"run": run, "attempt": attempt, "action": action, "ticket": target}
    if dry_run:
        result["payload"] = payload
        if action == "create-task":
            planned.add(run)
    else:
        result["receipt"] = invoke(action, payload, write=True)["receipt"]
        if not activity_matches(invoke, marker, owner, run, attempt=attempt):
            raise ValueError("Taskboard did not confirm the CI observation in Activity.")
    return result


def poll(read, invoke, state, *, since, board, owner, dry_run):
    context = invoke("context", {})
    if not board.startswith("https://") or context["board"].rstrip("/") != board.rstrip("/"):
        raise ValueError("Use the signed session for the configured HTTPS Taskboard.")
    if context["actor"]["id"] != owner:
        raise ValueError("Use the signed session for the configured ticket owner.")
    binding = {"board": board.rstrip("/"), "owner": owner}
    saved = json.loads(state.read_text()) if state.is_file() else {**binding, "since": since, "attempts": {}, "pending": []}
    if any(saved.get(key) != value for key, value in binding.items()):
        raise ValueError("Use the original owner and board for this watcher state.")
    if since < saved["since"]:
        raise ValueError("The monitoring window may only move forward.")
    attempts = saved["attempts"]
    pending = set(saved["pending"])
    incomplete = set(saved.get("incomplete", []))
    results, planned = [], set()
    # Finish reconciling the old window once before a renewal can drop its runs.
    events = list(observations(read, saved["since"], attempts, pending | incomplete))
    if since > saved["since"]:
        # The new window is a subset. A failed old run remains in pending until it passes.
        saved["since"] = since
    for event in events:
        if result := reconcile(invoke, event, owner=owner, dry_run=dry_run, planned=planned):
            results.append(result)
        if not dry_run:
            run, attempt, _, outcome, _ = event
            if outcome == "incomplete":
                incomplete.add(run)
                continue
            incomplete.discard(run)
            if outcome != "missing_failed_jobs":
                attempts[str(run)] = attempt
            if outcome in ("failure", "missing_failed_jobs"):
                pending.add(run)
            elif outcome == "success":
                pending.discard(run)
    if not dry_run:
        saved["pending"] = sorted(pending)
        saved["incomplete"] = sorted(incomplete)
        write_json(state, saved)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True, help="Private, dedicated watcher state directory.")
    parser.add_argument("--since", type=date.fromisoformat, required=True, help="Monitor runs created on or after this UTC date.")
    parser.add_argument("--board", required=True, help="HTTPS Taskboard base URL.")
    parser.add_argument("--owner", required=True, help="Exact signed owner ID from Taskboard context.")
    parser.add_argument("--service", default="tasks-board")
    parser.add_argument("--public-github", action="store_true", help="Read public GitHub REST without a GitHub login.")
    parser.add_argument("--apply", action="store_true", help="Enable signed writes. Omit for read-only preview.")
    args = parser.parse_args()
    read = public_github_read if args.public_github else github_read
    try:
        state = args.state / "main-ci.json"
        options = {"since": args.since.isoformat(), "board": args.board, "owner": args.owner, "dry_run": not args.apply}
        invoke = signed_in_service(args.service)
        if args.apply:
            args.state.mkdir(mode=0o700, parents=True, exist_ok=True)
            with queue_lock(args.state):
                results = poll(read, invoke, state, **options)
        else:
            results = poll(read, invoke, state, **options)
    except GitHubRateLimited as error:
        print(json.dumps({"dry_run": not args.apply, "status": "rate-limited", "retry_at": error.retry_at}))
        return 75
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Taskboard main CI watcher: {error}\n")
    print(json.dumps({"dry_run": not args.apply, "observations": results}))


if __name__ == "__main__":
    raise SystemExit(main())
