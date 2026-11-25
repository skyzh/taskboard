"""Readable project destinations and optional per-project repository settings."""

import json
import re

from taskboard_client.references import prefix
from tasks_extensions import EVENTS, REPOSITORY


REPOSITORY_LINKS = {
    "github": "https://github.com/",
    "stamp": "https://stamp.build/github/",
}


_PUBLIC_RAFT_CHANNEL = re.compile(
    r"https://(?:app\.)?raft\.build/s/(?P<server>[A-Za-z0-9][A-Za-z0-9_-]*)/channel/"
    r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}"
)
_RAFT_CHANNEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def public_raft_links(raw):
    """Read browser links that the board operator has confirmed are public."""
    if raw is None:
        return {}
    try:
        values = json.loads(raw)
        if not isinstance(values, dict):
            raise ValueError("Map project prefixes to public Raft channel URLs.")
        links = {}
        for project, value in values.items():
            project = prefix(project)
            channel = None
            if isinstance(value, dict):
                if (value.keys() != {"url", "channel"} or not isinstance(value["channel"], str)
                        or not _RAFT_CHANNEL_NAME.fullmatch(value["channel"])):
                    raise ValueError("A named entry needs an HTTPS Raft channel URL and channel name.")
                url, channel = value["url"], value["channel"]
            else:
                url = value
            route = _PUBLIC_RAFT_CHANNEL.fullmatch(url) if isinstance(url, str) else None
            if project in links or route is None:
                raise ValueError("Configure each project once with an HTTPS Raft channel URL.")
            links[project] = {"kind": "raft", "url": url}
            if channel is not None:
                links[project]["name"] = f"{route['server']}/{channel}"
        return links
    except ValueError as error:
        raise ValueError(f"Invalid TASKS_PUBLIC_RAFT_CHANNEL_URLS. {error}") from None


def setting_key(project, kind):
    return f"project:{project}:link:{kind}"


def validate(data):
    result = {}
    if not data:
        raise ValueError("Provide a GitHub or Stamp repository.")
    for kind, value in data.items():
        if value != "" and (not isinstance(value, str) or not REPOSITORY.fullmatch(value)
                            or value.split("/")[1] in (".", "..")):
            raise ValueError(f"Provide {kind} as owner/repository, or an empty value to clear it.")
        result[kind] = value.lower()
    return result


def save(db, project, values):
    for kind, repository in values.items():
        key = setting_key(project, kind)
        if repository:
            db.execute(
                """INSERT INTO board_settings (key, value) VALUES (?, ?)
                   ON CONFLICT (key) DO UPDATE SET value = excluded.value WHERE board_settings.value <> excluded.value""",
                (key, repository),
            )
        else:
            db.execute("DELETE FROM board_settings WHERE key = ?", (key,))


def read(db, project, configured, raft_link):
    saved = dict(db.execute("SELECT key, value FROM board_settings WHERE key IN (?, ?)",
                            tuple(setting_key(project, kind) for kind in REPOSITORY_LINKS)).fetchall())
    git_events = configured.get(EVENTS)
    repositories = {kind: saved.get(setting_key(project, kind)) for kind in REPOSITORY_LINKS}
    if git_events and repositories["github"] is None:
        repositories["github"] = git_events.repository
    links = [raft_link] if raft_link else []
    for kind, origin in REPOSITORY_LINKS.items():
        repository = repositories[kind]
        if repository:
            links.append({"kind": kind, "url": origin + repository, "name": repository})
    return {"links": links}
