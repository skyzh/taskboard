"""Readable project destinations and optional per-project repository settings."""

from tasks_extensions import EVENTS, REPOSITORY


REPOSITORY_LINKS = {
    "github": "https://github.com/",
    "stamp": "https://stamp.build/github/",
}


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


def read(db, project, configured, raft_url):
    saved = dict(db.execute("SELECT key, value FROM board_settings WHERE key IN (?, ?)",
                            tuple(setting_key(project, kind) for kind in REPOSITORY_LINKS)).fetchall())
    git_events = configured.get(EVENTS)
    repositories = {kind: saved.get(setting_key(project, kind)) for kind in REPOSITORY_LINKS}
    if git_events and repositories["github"] is None:
        repositories["github"] = git_events.repository
    links = [{"kind": "raft", "url": raft_url}] if raft_url else []
    for kind, origin in REPOSITORY_LINKS.items():
        if repositories[kind]:
            links.append({"kind": kind, "url": origin + repositories[kind]})
    return {"links": links}
