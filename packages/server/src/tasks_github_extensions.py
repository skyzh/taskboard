"""Refresh project GitHub extensions and match deployments to linked PRs."""

import json
from pathlib import Path

import tasks_core as ledger
import tasks_extensions as extensions
from tasks_github import (
    _ReadError, _source, _time, activity_read_bound, activity_rules, link_settings, observe_metadata, repository,
    save_updates,
)
from tasks_release_info import release_info
import tasks_storage


_BATCH_SIZE = 10
_SOURCE_READ_BUDGET = 30
_COMPARISONS_PER_REFRESH = 10


def _metadata_sources(db, configured):
    filters, args = [], []
    for project, config in configured.items():
        args.append(project)
        if config.get(extensions.STATUS):
            filters.append("tasks.project_prefix = ?")
        else:
            rule = config[extensions.EVENTS]
            target = f"https://github.com/{rule.repository}/"
            if not rule.activity:
                target += "pull/"
            filters.append("(tasks.project_prefix = ? AND SUBSTR(LOWER(sources.target), 1, ?) = ?)")
            args.extend((len(target), target))
    scope = " OR ".join(filters)
    return db.execute(
        f"""SELECT sources.*
            FROM sources LEFT JOIN github_source_status AS status ON status.source_id = sources.id
            WHERE sources.kind = 'github' AND EXISTS (
                SELECT 1 FROM task_sources JOIN tasks ON tasks.id = task_sources.task_id
                WHERE task_sources.source_id = sources.id AND ({scope})
            )
            ORDER BY COALESCE(status.attempted_at, ''), sources.id LIMIT ?""",
        (*args, _BATCH_SIZE),
    ).fetchall()


def _deployment_candidates(db, project):
    return db.execute(
        """SELECT tasks.id AS task_id, sources.id AS source_id, sources.target, status.merge_commit, status.merged_at
           FROM tasks JOIN task_sources ON task_sources.task_id = tasks.id
           JOIN sources ON sources.id = task_sources.source_id
           JOIN github_source_status AS status ON status.source_id = sources.id
           WHERE tasks.project_prefix = ? AND status.merge_commit IS NOT NULL
           ORDER BY sources.id, tasks.id""", (project,),
    ).fetchall()


def _deployed(db, reader, compare, project, rule, running_release, board_url, result):
    if rule.deployment.provider == "github":
        deployed = reader.deployed_commit(rule)
    else:
        commit, at = running_release.get("commit"), _time(running_release.get("deployed_at"))
        if not commit or not at:
            result["errors"].append({"project": project, "error": "pi_release_unverified"})
            return
        deployed = commit, at, board_url.rstrip("/") + "/api/v1/release"
    if deployed is None:
        return
    commit, at, url = deployed
    environment = rule.deployment.environment
    cache_key = f"github-deployment:{project}:{rule.repository}:{rule.deployment.provider}:{environment}"
    saved = db.execute("SELECT value FROM board_settings WHERE key = ?", (cache_key,)).fetchone()
    cache = json.loads(saved["value"]) if saved else {}
    matches = cache["matches"] if cache.get("commit") == commit else {}
    for row in _deployment_candidates(db, project):
        item = _source(row["target"])
        if repository(item) != rule.repository or row["merged_at"] > at:
            continue
        merged = row["merge_commit"]
        key = f"deploy:{rule.deployment.provider}:{environment}:{merged}"
        if extensions.already_recorded(db, row["task_id"], row["source_id"], key):
            continue
        if merged not in matches:
            included = compare(rule.repository, merged, commit)
            if included is None:
                continue
            matches[merged] = included
            with tasks_storage.write(db):
                db.execute(
                    "INSERT INTO board_settings VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (cache_key, json.dumps({"commit": commit, "matches": matches})),
                )
        if not matches[merged]:
            continue
        evidence = {
            "kind": "deployed", "at": at, "note": f"Pull request #{item.number} deployed to {environment}.",
            "links": [("Deployment evidence", url), ("GitHub pull request", item.url)],
            "context": {"merge_commit": merged, "deployed_commit": commit, "environment": environment,
                        "github_source": item.url},
        }
        with tasks_storage.write(db):
            linked = db.execute("SELECT 1 FROM task_sources WHERE task_id = ? AND source_id = ?",
                                (row["task_id"], row["source_id"])).fetchone()
            if linked and extensions.record(db, row["task_id"], row["source_id"], key, evidence, rule.deployment.actions):
                ledger.bump_revision(db)
                result["deployments"] += 1


def _read_observations(item, reader, settings, remaining):
    rules = activity_rules(settings, item)
    if rules:
        issue_status = any(options.get(extensions.STATUS) for options in settings.values())
        review_comments = any("review_comment" in policies for policies in rules.values())
        bound = activity_read_bound(item, issue_status=issue_status, review_comments=review_comments)
        if bound > remaining:
            return None
        return bound, reader._read_item(item, issue_status=issue_status, review_comments=review_comments)
    if remaining < 1:
        return None
    try:
        return 1, ((), None, (), reader.read_metadata(item))
    except _ReadError as error:
        return 1, ((), None, (("status", str(error)),), None)


def _ci_policies(settings, item):
    if item.kind != "pull":
        return {}
    return {
        number: rule.ci_jobs for number, options in settings.items()
        if (rule := options.get(extensions.EVENTS)) and rule.repository == repository(item) and rule.ci_jobs is not None
    }


def refresh(db, reader, configured, *, release_source=None, board_url):
    """Run one bounded provider read and project rule pass outside service requests."""
    configured = {key: value for key, value in configured.items()
                  if value.get(extensions.STATUS) or value.get(extensions.EVENTS)}
    result = {"sources": 0, "activity": 0, "deployments": 0, "errors": []}
    if not configured:
        return result
    remaining_reads = _SOURCE_READ_BUDGET
    for source in _metadata_sources(db, configured):
        checked_at = ledger.timestamp()
        try:
            item = _source(source["target"])
        except _ReadError as error:
            result["errors"].append({"source": source["source_key"], "error": str(error)})
            with tasks_storage.write(db):
                extensions.save_status(db, source["id"], None, checked_at)
                ledger.bump_revision(db)
            continue
        settings = link_settings(db, source["id"], configured)
        read_ci = bool(_ci_policies(settings, item))
        observation = _read_observations(item, reader, settings, remaining_reads - int(read_ci))
        if observation is None:
            # Leave the next source unattempted so it starts the following pass.
            break
        reads, (updates, review_state, errors, metadata) = observation
        remaining_reads -= reads
        checks = ()
        if read_ci and metadata is not None:
            remaining_reads -= 1
            try:
                checks, warning = reader.completed_ci_jobs(item, metadata)
            except _ReadError as error:
                warning = str(error)
            if warning:
                errors = (*errors, ("checks", warning))
        result["errors"].extend({"source": source["source_key"], "error": error} for _, error in errors)
        with tasks_storage.write(db):
            settings = link_settings(db, source["id"], configured)
            merged = observe_metadata(db, source, item, metadata, settings, checked_at)
            result["activity"] += save_updates(
                db, source, item, updates, settings, activity_rules(settings, item),
                review_state=review_state, merged=merged,
            )
            for number, policy in _ci_policies(settings, item).items():
                for key, evidence in checks:
                    result["activity"] += int(extensions.record(db, number, source["id"], key, evidence, policy))
            ledger.bump_revision(db)
        result["sources"] += 1
        if any(error == "github_rate_limited" for _, error in errors):
            return result
    running_release = release_info(Path(release_source)) if release_source is not None else {}
    remaining = _COMPARISONS_PER_REFRESH

    def compare(repository, merged, deployed):
        nonlocal remaining
        if merged == deployed:
            return True
        if not remaining:
            return None
        remaining -= 1
        return reader.includes_commit(repository, merged, deployed)

    for project, options in configured.items():
        rule = options.get(extensions.EVENTS)
        if rule and rule.deployment:
            try:
                _deployed(db, reader, compare, project, rule, running_release, board_url, result)
            except _ReadError as error:
                result["errors"].append({"project": project, "error": str(error)})
                if str(error) == "github_rate_limited":
                    break
    return result
