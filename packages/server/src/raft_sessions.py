"""Store established Raft sessions outside the Tasks ledger."""

from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
import os
from pathlib import Path
import sqlite3
import stat
from time import time
from typing import Literal


@dataclass(frozen=True)
class Session:
    token: str = field(repr=False)
    expires: float
    subject: str
    kind: Literal["human", "agent"]
    server_id: str | None = None


class MemorySessions:
    def __init__(self):
        self.sessions = {}

    def get(self, key, now):
        return self.get_digest(session_digest(key), now)

    def get_digest(self, digest, now):
        session = self.sessions.get(digest)
        if session is not None and session.expires <= now:
            self.discard_digest(digest)
            return None
        return session

    def discard(self, key):
        if key:
            self.discard_digest(session_digest(key))

    def discard_digest(self, digest):
        self.sessions.pop(digest, None)

    def replace(self, old_key, key, session, now):
        self.sessions = {item: value for item, value in self.sessions.items() if value.expires > now}
        self.discard(old_key)
        self.sessions[session_digest(key)] = session


def session_digest(key):
    return sha256(key.encode()).hexdigest()


class DurableSessions:
    def __init__(self, directory, audience, *, marketplace=False, filename=None):
        folder = Path(directory)
        if not folder.is_absolute() or not hasattr(os, "geteuid"):
            raise ValueError("Board session storage requires an absolute private directory on Unix.")
        folder.mkdir(mode=0o700, exist_ok=True)
        self._check_private(folder, stat.S_ISDIR, 0o700)
        # Keep the existing fixed-server database readable by older deployments.
        self.path = folder / (filename or ("raft-marketplace-sessions.sqlite3" if marketplace else "raft-sessions.sqlite3"))
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        self._check_private(self.path, stat.S_ISREG, 0o600)
        self.audience = audience
        self._marketplace = marketplace
        server_column = "server_id TEXT NOT NULL," if marketplace else ""
        with self._connect() as db:
            db.execute(
                f"""
                CREATE TABLE IF NOT EXISTS sessions (
                    audience TEXT NOT NULL,
                    cookie_digest TEXT NOT NULL,
                    token TEXT NOT NULL,
                    expires REAL NOT NULL,
                    subject TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    {server_column}
                    PRIMARY KEY (audience, cookie_digest)
                )
                """
            )
            self._prune(db, time())

    @staticmethod
    def _check_private(path, expected_type, mode):
        info = path.lstat()
        if (not expected_type(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != mode):
            raise ValueError("Board session storage must be owned by the service user with directory mode 0700 and file mode 0600.")

    @contextmanager
    def _connect(self):
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True)) as db, db:
            yield db

    def _prune(self, db, now):
        db.execute("DELETE FROM sessions WHERE audience = ? AND expires <= ?", (self.audience, now))

    def get(self, key, now):
        return self.get_digest(session_digest(key), now)

    def get_digest(self, digest, now):
        server_column = ", server_id" if self._marketplace else ""
        with self._connect() as db:
            row = db.execute(
                f"""
                SELECT token, expires, subject, kind{server_column} FROM sessions
                WHERE audience = ? AND cookie_digest = ? AND expires > ?
                """, (self.audience, digest, now),
            ).fetchone()
        return Session(*row) if row else None

    def discard(self, key):
        if key:
            self.discard_digest(session_digest(key))

    def discard_digest(self, digest):
        with self._connect() as db:
            self._discard(db, digest)

    def _discard(self, db, digest):
        db.execute(
            "DELETE FROM sessions WHERE audience = ? AND cookie_digest = ?",
            (self.audience, digest),
        )

    def replace(self, old_key, key, session, now):
        values = (self.audience, session_digest(key), session.token, session.expires, session.subject, session.kind)
        if self._marketplace:
            values += (session.server_id,)
        placeholders = ", ".join("?" for _ in values)
        with self._connect() as db:
            self._prune(db, now)
            if old_key:
                self._discard(db, session_digest(old_key))
            db.execute(f"INSERT INTO sessions VALUES ({placeholders})", values)
