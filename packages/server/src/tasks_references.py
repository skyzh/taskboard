"""Recognize pasted ticket references and provider links without opening them."""

import re
from urllib.parse import parse_qsl, urlsplit


PROJECT = r"[A-Za-z][A-Za-z0-9]{0,7}"
NUMBER = r"[0-9]{1,19}"
FRAGMENT = re.compile(rf"task-(?:({PROJECT})-)?({NUMBER})")
TOKEN = re.compile(
    rf"""(?P<url>https?://[^\s<>()\[\]{{}}'"\x60]+)"""
    rf"|(?P<anchor>(?<![\w/#-])#task-(?:{PROJECT}-)?{NUMBER})(?![\w/-])"
    rf"|(?<![\w/-])(?P<project>{PROJECT})-(?P<number>{NUMBER})(?![\w/-])",
    re.IGNORECASE,
)
UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
RAFT_ROUTE = re.compile(rf"/s/[a-zA-Z0-9_-]+/(?:channel|dm)/({UUID})")
RAFT_MESSAGE = re.compile(UUID)
STAMP_PR = re.compile(
    r"/(?:s/[a-zA-Z0-9_-]+/)?github/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]{0,18})"
    r"(?:/approvals/[0-9a-fA-F]+/evidence)?/?"
)


def trim_url(value):
    return value.rstrip(".,;!?:。；，！")


def urls(text):
    return (trim_url(match["url"]) for match in TOKEN.finditer(text) if match["url"])


def web_url(url):
    try:
        parts = urlsplit(url)
        if parts.scheme in ("http", "https") and parts.hostname and parts.username is None and "\\" not in url:
            return parts
    except ValueError:
        pass
    return None


def ticket_keys(text, board):
    for token in TOKEN.finditer(text):
        if token["project"]:
            yield token["project"].upper(), int(token["number"])
            continue
        if token["url"]:
            link = web_url(trim_url(token["url"]))
            if link is None or (
                link.scheme, link.netloc.lower(), link.path.rstrip("/")
            ) != (board.scheme, board.netloc.lower(), board.path.rstrip("/")):
                continue
            fragment = link.fragment
        else:
            fragment = token["anchor"][1:]
        if match := FRAGMENT.fullmatch(fragment):
            yield (match[1] or "T").upper(), int(match[2])


def pasted_source(url):
    """Return a distinct destination key and a default label for supported links."""
    link = web_url(url)
    if not link or link.scheme != "https":
        return None
    host = link.netloc.lower()
    if host == "github.com":
        from tasks_github import linked_source

        try:
            key, canonical = linked_source("https://github.com" + link.path)
        except ValueError:
            return None
        repository, number = key.split("#")
        kind = "Pull request" if "/pull/" in canonical else "Issue"
        return (canonical, link.query, link.fragment), f"{kind} {repository} #{number}"
    if host == "stamp.build" and (match := STAMP_PR.fullmatch(link.path)):
        return (host, link.path.rstrip("/"), link.query, link.fragment), f"Stamp PR {match[1]} #{match[2]}"
    if host == "app.raft.build" and (route := RAFT_ROUTE.fullmatch(link.path)):
        query = parse_qsl(link.query, keep_blank_values=True)
        fields = dict(query)
        if len(query) != len(fields) or not RAFT_MESSAGE.fullmatch(fields.get("msg", "")):
            return None
        thread = fields.get("thread")
        if fields.keys() - {"msg", "thread"} or (
            thread is not None and not re.fullmatch(rf"{route[1]}:{UUID}", thread)
        ):
            return None
        return (host, link.path, fields["msg"], thread, link.fragment), "Raft conversation"
    return None
