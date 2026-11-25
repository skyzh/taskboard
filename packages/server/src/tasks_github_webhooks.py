"""Authenticate GitHub deliveries and queue refreshes of linked pull requests."""

from hashlib import sha256
from hmac import compare_digest, digest
import json
from uuid import UUID

import tasks_extensions as extensions
import tasks_projects as projects
import tasks_storage


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS github_webhook_deliveries (
        project TEXT NOT NULL REFERENCES projects (prefix),
        delivery TEXT NOT NULL,
        PRIMARY KEY (project, delivery)
    )""",
    """CREATE TABLE IF NOT EXISTS github_webhook_refresh (
        project TEXT NOT NULL REFERENCES projects (prefix),
        source_id INTEGER NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
        delivery TEXT NOT NULL,
        queued_at TEXT NOT NULL,
        PRIMARY KEY (project, source_id)
    )""",
)
PULL_ACTIONS = {"opened", "reopened", "synchronize", "closed"}


def configuration(raw, configured):
    """Return webhook secrets for projects with an existing GitHub events rule."""
    if raw is None:
        return {}
    try:
        mapping = json.loads(raw)
        if not isinstance(mapping, dict) or not mapping:
            raise ValueError
        result = {}
        for key, secret in mapping.items():
            project = projects.prefix(key)
            if (project in result or not configured.get(project, {}).get(extensions.EVENTS)
                    or not isinstance(secret, str) or not secret.strip()):
                raise ValueError
            result[project] = secret.encode()
        return result
    except (ValueError, UnicodeError):
        raise ValueError(
            "Map project prefixes with a github-events rule to secrets in TASKS_GITHUB_WEBHOOK_SECRETS."
        ) from None


def authenticated(secret, body, signature):
    expected = b"sha256=" + digest(secret, body, sha256).hex().encode("ascii")
    return compare_digest(expected, signature.encode("utf-8"))


def delivery_id(value):
    number = str(UUID(value))
    if value.lower() != number:
        raise ValueError("Send a GitHub delivery ID.")
    return number


def target(event, payload, repository):
    """Return a linked PR URL prefix for pushes or an exact URL for pull requests."""
    if not isinstance(payload, dict):
        raise ValueError("Send a GitHub JSON object.")
    remote = payload.get("repository")
    name = remote.get("full_name") if isinstance(remote, dict) else None
    if not isinstance(name, str) or name.lower() != repository:
        raise PermissionError("GitHub repository does not match the project rule.")
    prefix = f"https://github.com/{repository}/pull/"
    if event == "push":
        ref, revision = payload.get("ref"), payload.get("after")
        if payload.get("deleted") is True or (isinstance(ref, str) and not ref.startswith("refs/heads/")):
            return None
        if (not isinstance(ref, str) or ref == "refs/heads/" or not isinstance(revision, str)
                or not extensions.COMMIT.fullmatch(revision) or revision == "0" * 40):
            raise ValueError("Send a branch ref and commit revision for a GitHub push.")
        return prefix, False
    action = payload.get("action")
    if event != "pull_request" or not isinstance(action, str) or action not in PULL_ACTIONS:
        return None
    pull = payload.get("pull_request")
    number = payload.get("number")
    head = pull.get("head") if isinstance(pull, dict) else None
    revision = head.get("sha") if isinstance(head, dict) else None
    if (type(number) is not int or not 0 < number < 10**19 or not isinstance(pull, dict)
            or pull.get("number") != number or not isinstance(revision, str) or not extensions.COMMIT.fullmatch(revision)):
        raise ValueError("Send the exact GitHub pull request number and head revision.")
    return prefix + str(number), True


def enqueue(db, project, delivery, target_url, *, exact, timestamp):
    with tasks_storage.write(db):
        projects.require(db, project)
        if not db.execute(
            "INSERT INTO github_webhook_deliveries VALUES (?, ?) ON CONFLICT DO NOTHING",
            (project, delivery),
        ).rowcount:
            return None
        match = "LOWER(RTRIM(sources.target, '/')) = ?" if exact else "SUBSTR(LOWER(sources.target), 1, ?) = ?"
        args = (target_url,) if exact else (len(target_url), target_url)
        return db.execute(
            f"""INSERT INTO github_webhook_refresh (project, source_id, delivery, queued_at)
                SELECT ?, sources.id, ?, ? FROM sources
                WHERE sources.kind = 'github' AND {match} AND EXISTS (
                    SELECT 1 FROM task_sources JOIN tasks ON tasks.id = task_sources.task_id
                    WHERE task_sources.source_id = sources.id AND tasks.project_prefix = ?
                )
                ON CONFLICT (project, source_id) DO UPDATE SET delivery = excluded.delivery""",
            (project, delivery, timestamp, *args, project),
        ).rowcount


def pending(db, limit):
    if not tasks_storage.has_table(db, "github_webhook_refresh"):
        return ()
    return db.execute(
        """SELECT sources.*, queued.project, queued.delivery FROM github_webhook_refresh AS queued
           JOIN sources ON sources.id = queued.source_id
           ORDER BY queued.queued_at, queued.project, queued.source_id LIMIT ?""",
        (limit,),
    ).fetchall()


def acknowledge(db, queued):
    # A concurrent delivery can replace the cue while the provider read runs.
    db.execute(
        "DELETE FROM github_webhook_refresh WHERE project = ? AND source_id = ? AND delivery = ?",
        (queued["project"], queued["id"], queued["delivery"]),
    )
