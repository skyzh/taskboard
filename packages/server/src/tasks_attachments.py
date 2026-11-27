"""Private object storage for ticket attachment metadata and bytes."""

import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol
from uuid import UUID


METADATA_SCHEMA = """CREATE TABLE IF NOT EXISTS ticket_attachments (
    id TEXT PRIMARY KEY,
    task_id BIGINT NOT NULL REFERENCES tasks (id),
    upload_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    backend TEXT NOT NULL,
    UNIQUE (task_id, upload_id)
)"""

BYTE_SCHEMA = """CREATE TABLE IF NOT EXISTS ticket_attachment_bytes (
    attachment_id TEXT PRIMARY KEY REFERENCES ticket_attachments (id) ON DELETE CASCADE,
    contents BYTEA NOT NULL
)"""


class Storage(Protocol):
    """The caller authorizes access and owns the metadata transaction."""

    def write(self, db, key: str, contents: bytes) -> None: ...
    def read(self, db, key: str) -> bytes: ...
    def delete(self, db, key: str) -> None: ...


class FilesystemStorage:
    """Use an operator-owned directory outside the served application tree."""

    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise NotADirectoryError(root)

    def _path(self, key: str) -> Path:
        if UUID(key).hex != key:
            raise ValueError("Expected an opaque attachment key.")
        return self.root / key

    def _sync_directory(self) -> None:
        if os.name == "posix":
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def write(self, _db, key: str, contents: bytes) -> None:
        path = self._path(key)
        with NamedTemporaryFile(dir=self.root, prefix=".upload-", delete=False) as handle:
            staged = Path(handle.name)
            try:
                handle.write(contents)
                handle.flush()
                os.fsync(handle.fileno())
                handle.close()
                # Publish a complete object without overwriting an existing one.
                os.link(staged, path)
                self._sync_directory()
            finally:
                staged.unlink(missing_ok=True)

    def read(self, _db, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, _db, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
        self._sync_directory()


class PostgresStorage:
    """Store bytes in the same transaction as the ticket metadata."""

    def write(self, db, key: str, contents: bytes) -> None:
        db.execute("INSERT INTO ticket_attachment_bytes (attachment_id, contents) VALUES (?, ?)", (key, contents))

    def read(self, db, key: str) -> bytes:
        row = db.execute("SELECT contents FROM ticket_attachment_bytes WHERE attachment_id = ?", (key,)).fetchone()
        if row is None:
            raise FileNotFoundError("Attachment bytes are unavailable.")
        return bytes(row["contents"])

    def delete(self, db, key: str) -> None:
        db.execute("DELETE FROM ticket_attachment_bytes WHERE attachment_id = ?", (key,))
