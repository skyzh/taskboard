"""Configuration and task effects for the board's bundled extensions."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re

from taskboard_client.references import prefix

import tasks_core as ledger
import tasks_projects as projects


STATUS = "github-status"
EVENTS = "github-events"
STAMP_REVIEWS = "stamp-reviews"
ACTOR = "GitHub extension"
STALE_AFTER = timedelta(hours=1)
REPOSITORY = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}")
ENVIRONMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ./_-]{0,79}")
COMMIT = re.compile(r"[0-9a-f]{40}")
ACTIVITY_TYPE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS github_source_status (
        source_id INTEGER PRIMARY KEY REFERENCES sources (id),
        state TEXT,
        checked_at TEXT,
        attempted_at TEXT NOT NULL,
        failed INTEGER NOT NULL,
        merge_commit TEXT,
        merged_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS extension_deliveries (
        task_id INTEGER NOT NULL REFERENCES tasks (id),
        source_id INTEGER NOT NULL REFERENCES sources (id),
        observation TEXT NOT NULL,
        PRIMARY KEY (task_id, source_id, observation)
    )""",
    """CREATE TABLE IF NOT EXISTS extension_notifications (
        event_id INTEGER PRIMARY KEY REFERENCES events (id),
        recipient TEXT NOT NULL REFERENCES board_principals (ledger_key)
    )""",
    "CREATE INDEX IF NOT EXISTS extension_notifications_recipient ON extension_notifications (recipient, event_id)",
    """CREATE TABLE IF NOT EXISTS stamp_review_health (
        project TEXT NOT NULL REFERENCES projects (prefix),
        repository TEXT NOT NULL,
        pull TEXT NOT NULL,
        checked_at TEXT NOT NULL,
        succeeded_at TEXT,
        failure TEXT,
        PRIMARY KEY (project, repository)
    )""",
)


@dataclass(frozen=True)
class Actions:
    complete: bool = False
    notify_dri: bool = False


@dataclass(frozen=True)
class Deployment:
    provider: str
    environment: str
    actions: Actions


@dataclass(frozen=True)
class GitHubEvents:
    repository: str
    merge: Actions
    deployment: Deployment | None
    activity: dict[str, Actions]


def _actions(config):
    if (not isinstance(config, dict) or config.keys() - {"complete", "notify_dri"}
            or any(type(value) is not bool for value in config.values())):
        raise ValueError("Use boolean complete and notify_dri rule settings.")
    return Actions(**config)


def _events(config):
    if (not isinstance(config, dict) or config.keys() - {"repository", "merge", "deployment", "activity"}
            or not isinstance(config.get("repository"), str) or not REPOSITORY.fullmatch(config["repository"])
            or config["repository"].split("/")[1] in (".", "..")):
        raise ValueError("Name a public GitHub repository as owner/repository.")
    merge = _actions(config.get("merge", {}))
    deployment = None
    if "deployment" in config:
        value = config["deployment"]
        if (not isinstance(value, dict) or value.keys() - {"provider", "environment", "complete", "notify_dri"}
                or value.get("provider") not in ("pi", "github") or not isinstance(value.get("environment"), str)
                or not ENVIRONMENT.fullmatch(value["environment"])):
            raise ValueError("Choose a deployment provider (pi or github) and an environment.")
        actions = _actions({key: value[key] for key in ("complete", "notify_dri") if key in value})
        deployment = Deployment(value["provider"], value["environment"], actions)
    activity = config.get("activity", {})
    if not isinstance(activity, dict) or any(
        kind in {"merged", "pull-merged", "deployed"} or not ACTIVITY_TYPE.fullmatch(kind) for kind in activity
    ):
        raise ValueError("Choose GitHub activity event names. Configure merges and deployments separately.")
    return GitHubEvents(config["repository"].lower(), merge, deployment, {
        kind: _actions(actions) for kind, actions in activity.items()
    })


def _stamp_repository(value):
    if (not isinstance(value, str) or not REPOSITORY.fullmatch(value)
            or value.split("/")[1] in (".", "..")):
        raise ValueError("Choose the Stamp-linked GitHub repository as owner/repository.")
    return value.lower()


REGISTRY = {STATUS: lambda value: value is True, EVENTS: _events, STAMP_REVIEWS: _stamp_repository}


def configuration(raw):
    if raw is None:
        return {}
    try:
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise ValueError("Map project prefixes to bundled extensions.")
        parsed = {}
        for project, extensions in document.items():
            project = prefix(project)
            if project in parsed or not isinstance(extensions, dict) or extensions.keys() - REGISTRY.keys():
                raise ValueError("Configure each project once and choose bundled extensions.")
            if STATUS in extensions and type(extensions[STATUS]) is not bool:
                raise ValueError("Use true or false to enable or disable GitHub status.")
            parsed[project] = {key: REGISTRY[key](value) for key, value in extensions.items()}
        return parsed
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid TASKS_PROJECT_EXTENSIONS. {error}") from None


def source_states(db, number, now=None):
    now = now or datetime.now(timezone.utc)
    rows = db.execute(
        """SELECT target, state, checked_at, failed
           FROM task_sources JOIN sources ON sources.id = task_sources.source_id
           LEFT JOIN github_source_status ON github_source_status.source_id = sources.id
           WHERE task_sources.task_id = ? AND sources.kind = 'github'""", (number,),
    )
    return {
        row["target"]: {
            "state": row["state"], "checked_at": row["checked_at"],
            "stale": bool(row["state"] and (row["failed"] or
                                           now - datetime.fromisoformat(row["checked_at"]) >= STALE_AFTER)),
        }
        for row in rows
    }


def save_status(db, source, state, checked_at, *, merge_commit=None, merged_at=None):
    db.execute(
        """INSERT INTO github_source_status VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (source_id) DO UPDATE SET
             state = COALESCE(excluded.state, github_source_status.state),
             checked_at = COALESCE(excluded.checked_at, github_source_status.checked_at),
             attempted_at = excluded.attempted_at,
             failed = excluded.failed,
             merge_commit = COALESCE(excluded.merge_commit, github_source_status.merge_commit),
             merged_at = COALESCE(excluded.merged_at, github_source_status.merged_at)
           WHERE excluded.attempted_at >= github_source_status.attempted_at""",
        (source, state, checked_at if state else None, checked_at, int(state is None), merge_commit, merged_at),
    )


def already_recorded(db, task_id, source_id, observation):
    return db.execute(
        "SELECT 1 FROM extension_deliveries WHERE task_id = ? AND source_id = ? AND observation = ?",
        (task_id, source_id, observation),
    ).fetchone() is not None


def record_observation(db, task_id, source_id, observation, actor, note, links, context):
    if not db.execute(
        "INSERT INTO extension_deliveries VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
        (task_id, source_id, observation),
    ).rowcount:
        return None
    task = ledger.get_task(db, task_id)
    event = ledger.record(db, task_id, "log", actor, task["state"], note, None, links, context)
    return task, event


def record(db, task_id, source_id, observation, evidence, actions):
    context = {"extension": EVENTS, "github_event": evidence["kind"], "provider_at": evidence["at"],
               **evidence["context"]}
    saved = record_observation(
        db, task_id, source_id, observation, ACTOR, evidence["note"], evidence["links"], context,
    )
    if saved is None:
        return False
    task, event = saved
    if actions.notify_dri:
        db.execute(
            """INSERT INTO extension_notifications (event_id, recipient)
               SELECT ?, ledger_key FROM board_principals WHERE ledger_key = ? AND kind = 'agent'""",
            (event, task["dri"]),
        )
    if (actions.complete and task["state"] in ledger.TRANSITIONS["done"][0]
            and ledger.task_goal_kind(db, task_id) != "standing" and not ledger.peer_asks.find(db, task_id)):
        ledger.complete_from_extension(db, task, ACTOR, evidence["note"], evidence["links"], context)
    return True
