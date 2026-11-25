"""The board's document routes and safe post-sign-in destinations."""

import re
from urllib.parse import unquote, urlsplit

from werkzeug.routing import BaseConverter


NUMBER = r"[1-9][0-9]{0,18}"
RECORD = rf"(?:[A-Z][A-Z0-9]{{0,7}}-)?{NUMBER}"
OWNER = r"(?!\.{1,2}$)[^/\\\x00-\x1f\x7f]{1,160}"
PAGE = (
    rf"(?:reports(?:/agents/{OWNER})?|tasks|tickets/{RECORD}|"
    rf"activity(?:/events/{NUMBER})?|my-activity|knowledge(?:/{RECORD})?|"
    rf"schedules(?:/R-{NUMBER})?|team-overview|stats|notifications|settings|"
    r"integrations|personal-codex|search|about)"
)
DOCUMENT_PATH = re.compile(rf"/(?:{PAGE})?")
LOCAL_ANCHOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}")
INVALID_CHAR = re.compile(r"[\\\x00-\x20\x7f]")


class BoardPage(BaseConverter):
    regex = PAGE
    part_isolating = False


def board_return_allowed(target):
    """Check a mount-relative document URL before storing it for sign-in."""
    if (not target.startswith("/") or target.startswith("//")
            or len(target) > 2048 or INVALID_CHAR.search(target)):
        return False
    parts = urlsplit(target)
    try:
        segments = [unquote(segment, errors="strict") for segment in parts.path.split("/")]
    except UnicodeError:
        return False
    if any("/" in segment or segment in (".", "..") for segment in segments):
        return False
    return bool(DOCUMENT_PATH.fullmatch("/".join(segments))
                and (not parts.fragment or LOCAL_ANCHOR.fullmatch(parts.fragment)))
