"""Resolve a project's coordination destination with the configured Raft adapter."""

from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID


@dataclass(frozen=True)
class VerifiedChannel:
    server_id: str
    channel_id: str
    name: str
    may_post: bool
    matches_project_audience: bool
    # The adapter supplies this only when the channel link is safe for the board audience.
    browser_url: str | None = None


def setting_key(project):
    return f"project:{project}:raft-channel"


def saved_channel(db, project):
    row = db.execute("SELECT value FROM board_settings WHERE key = ?", (setting_key(project),)).fetchone()
    return row["value"] if row else None


def verified_channel(lookup, server_id, project, channel_id):
    if lookup is None or server_id is None:
        return None
    try:
        channel = lookup(server_id, project, channel_id)
    except (LookupError, OSError, RuntimeError):
        return None
    if (not isinstance(channel, VerifiedChannel) or channel.server_id != server_id or channel.channel_id != channel_id
            or not isinstance(channel.name, str) or not channel.name.strip()
            or channel.may_post is not True or channel.matches_project_audience is not True):
        return None
    return channel


def resolve(lookup, server_id, project, channel_id):
    """Return only the verified channel ID and its current display name."""
    channel = verified_channel(lookup, server_id, project, channel_id)
    if channel is None:
        return None
    return {"id": channel.channel_id, "name": channel.name}


def browser_link(lookup, server_id, project, channel_id):
    channel = verified_channel(lookup, server_id, project, channel_id) if channel_id else None
    if channel is None or not isinstance(channel.browser_url, str):
        return None
    try:
        url = urlsplit(channel.browser_url)
    except ValueError:
        return None
    if url.scheme == "https" and url.netloc.lower() in ("raft.build", "app.raft.build"):
        return channel.browser_url
    return None


def describe(lookup, server_id, project, channel_id):
    if lookup is not None and server_id is not None and channel_id is None:
        return {"project": project, "state": "unconfigured", "channel": None}
    channel = resolve(lookup, server_id, project, channel_id) if channel_id else None
    return {"project": project, "state": "configured" if channel else "unavailable", "channel": channel}


def normalize(channel_id):
    return str(UUID(channel_id))


def save(db, project, channel_id):
    if channel_id is None:
        db.execute("DELETE FROM board_settings WHERE key = ?", (setting_key(project),))
    else:
        db.execute(
            "INSERT INTO board_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value WHERE board_settings.value <> excluded.value",
            (setting_key(project), channel_id),
        )
