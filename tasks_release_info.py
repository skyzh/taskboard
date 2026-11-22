"""Public version metadata for the currently running app artifact."""

from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path

from tasks_release_changelog import GIT_COMMIT


METADATA_NAME = "active-release.json"
FIELDS = ("commit", "built_at", "deployed_at")


def release_info(source):
    source = Path(source).resolve()
    unknown = dict.fromkeys(FIELDS)
    try:
        metadata = json.loads((source.parent / METADATA_NAME).read_text(encoding="utf-8"))
        digest = sha256((source / "SHA256SUMS").read_bytes()).hexdigest()
        if metadata["manifest"] != digest or not GIT_COMMIT.fullmatch(metadata["commit"]):
            return unknown
        data = {field: metadata[field] for field in FIELDS}
        for field in ("built_at", "deployed_at"):
            if datetime.fromisoformat(data[field]).utcoffset() != timedelta(0):
                return unknown
    except (OSError, KeyError, TypeError, ValueError):
        return unknown
    return data
