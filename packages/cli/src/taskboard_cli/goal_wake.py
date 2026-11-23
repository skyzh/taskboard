"""Select one goal cue and keep its carrier request safe across watcher restarts."""

from contextlib import contextmanager
from datetime import date
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
from uuid import NAMESPACE_URL, uuid5

from taskboard_client import SharedActionUnavailable, SharedReadError
from taskboard_cli.shared import task_url


GOAL_PROJECT = "T"
_DUE_KINDS = {"deadline", "daily", "standing-daily"}


def _ticket(value):
    return isinstance(value, str) and re.fullmatch(r"T-[1-9][0-9]{0,18}", value) is not None


def _checks(value):
    return type(value) is int and value >= 0


def _day(value):
    try:
        return date.fromisoformat(value).isoformat() == value
    except (TypeError, ValueError):
        return False


def _candidate(row, day, episode):
    offer = {
        "task": row["ticket"], "goal_day": day, "goal_kind": row["kind"],
        "goal_key": row["key"], "completed_checks": row["completed_checks"],
    }
    if row["kind"].startswith("standing-"):
        offer["goal_episode"] = episode
    return offer


class GoalWakeState:
    """Store queue episodes and ambiguously submitted reminders in an owner-bound file."""

    def __init__(self, path, board_url, owner):
        self.path = Path(path).expanduser().absolute()
        self.scope = f"{board_url}\n{owner}\n{GOAL_PROJECT}"
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        except OSError:
            raise ValueError("Cannot create the private goal watcher state file.") from None
        else:
            os.close(fd)
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("Choose a private regular goal watcher state file owned by this agent.")
        with self.transaction() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if tables and tables != {"handoff", "cue"}:
                raise ValueError("Choose a separate goal watcher state file.")
            db.execute("""
                CREATE TABLE IF NOT EXISTS handoff (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    scope TEXT NOT NULL, busy INTEGER NOT NULL,
                    episode INTEGER NOT NULL, drain_event INTEGER NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS cue (
                    key TEXT PRIMARY KEY, ticket TEXT NOT NULL, day TEXT NOT NULL,
                    completed_checks INTEGER NOT NULL, outcome TEXT NOT NULL, reminder TEXT
                )
            """)
            db.execute("INSERT OR IGNORE INTO handoff VALUES (1, ?, 0, 0, 0)", (self.scope,))
            if db.execute("SELECT scope FROM handoff WHERE id = 1").fetchone()[0] != self.scope:
                raise ValueError("The goal watcher state belongs to a different board or agent.")

    @contextmanager
    def transaction(self):
        try:
            db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True)
            try:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    yield db
            finally:
                db.close()
        except sqlite3.Error:
            raise RuntimeError("Cannot read or save the private goal watcher state.") from None

    def observe_busy(self):
        with self.transaction() as db:
            db.execute("UPDATE handoff SET busy = 1 WHERE id = 1 AND busy = 0")

    def select(self, snapshot):
        if snapshot["ordinary_busy"]:
            self.observe_busy()
        if snapshot["busy"]:
            return None
        day = snapshot["day"]
        drain_event = snapshot["drain_event"]
        with self.transaction() as db:
            busy, episode, previous = db.execute(
                "SELECT busy, episode, drain_event FROM handoff WHERE id = 1"
            ).fetchone()
            if not episode or busy or drain_event > previous:
                episode += 1
                db.execute("UPDATE handoff SET busy = 0, episode = ?, drain_event = ? WHERE id = 1",
                           (episode, max(drain_event, previous)))
            for row in snapshot["cues"]:
                offer = _candidate(row, day, episode)
                if self.available(db, offer):
                    return offer
            if not snapshot["standing"]:
                return None
            standing = snapshot["standing"]
            offer = _candidate({
                **standing, "kind": "standing-drain", "key": f"drain:{standing['ticket']}:{episode}",
            }, day, episode)
            return offer if self.available(db, offer) else None

    @staticmethod
    def available(db, offer):
        if db.execute("SELECT 1 FROM cue WHERE key = ?", (offer["goal_key"],)).fetchone():
            return False
        previous = db.execute(
            "SELECT MAX(completed_checks) FROM cue WHERE ticket = ? AND day = ?",
            (offer["task"], offer["goal_day"]),
        ).fetchone()[0]
        if previous is None:
            return True
        return offer["goal_kind"] == "standing-drain" and offer["completed_checks"] > previous

    def reserve(self, offer):
        """Claim the one send before contacting the carrier. An interruption stays unknown."""
        with self.transaction() as db:
            if not self.available(db, offer):
                return False
            db.execute("INSERT INTO cue VALUES (?, ?, ?, ?, 'unknown', NULL)", (
                offer["goal_key"], offer["task"], offer["goal_day"], offer["completed_checks"],
            ))
            if offer["goal_kind"] == "standing-daily":
                drain = f"drain:{offer['task']}:{offer['goal_episode']}"
                db.execute("INSERT OR IGNORE INTO cue VALUES (?, ?, ?, ?, 'coalesced', NULL)", (
                    drain, offer["task"], offer["goal_day"], offer["completed_checks"],
                ))
        return True

    def requested(self, offer, reminder):
        with self.transaction() as db:
            db.execute("UPDATE cue SET outcome = 'requested', reminder = ? WHERE key = ?",
                       (reminder, offer["goal_key"]))

    def reminder(self, offer):
        with self.transaction() as db:
            return db.execute("SELECT reminder FROM cue WHERE key = ?", (offer["goal_key"],)).fetchone()[0]


class GoalWake:
    def __init__(self, board, state):
        self.board = board
        self.state = state

    def lookup(self):
        try:
            result = self.board.invoke("my-goal-cues", {"project": GOAL_PROJECT})
        except SharedActionUnavailable as error:
            raise SharedReadError(str(error), reason="action-unavailable") from error
        if not self.valid_snapshot(result):
            raise SharedReadError("The board returned an invalid goal cue.", reason="queue-scope")
        return self.state.select(result)

    @staticmethod
    def valid_snapshot(result):
        if (not isinstance(result, dict) or type(result.get("busy")) is not bool
                or type(result.get("ordinary_busy")) is not bool or not _day(result.get("day"))
                or (result["ordinary_busy"] and not result["busy"])
                or not _checks(result.get("drain_event")) or not isinstance(result.get("cues"), list)):
            return False
        standing = result.get("standing")
        if standing is not None and (not isinstance(standing, dict) or not _ticket(standing.get("ticket"))
                                     or not _checks(standing.get("completed_checks"))):
            return False
        for cue in result["cues"]:
            if (not isinstance(cue, dict) or not _ticket(cue.get("ticket")) or cue.get("kind") not in _DUE_KINDS
                    or cue.get("day") != result["day"] or not _checks(cue.get("completed_checks"))):
                return False
            expected_kind = "deadline" if cue["kind"] == "deadline" else "daily"
            prefix = f"{expected_kind}:{cue['ticket']}:"
            key = cue.get("key")
            if not isinstance(key, str) or not key.startswith(prefix):
                return False
            day = key.removeprefix(prefix)
            if (not _day(day) or (expected_kind == "daily" and day != result["day"])
                    or (expected_kind == "deadline" and day > result["day"])):
                return False
        return True

    def notify(self, offer, can_wake, send):
        candidate = {key: value for key, value in offer.items() if key != "board"}
        reserved = False
        reminder = None

        def record(reference):
            nonlocal reminder
            reminder = reference

        def claim():
            nonlocal reserved
            if not can_wake() or self.lookup() != candidate:
                return False
            if candidate["goal_kind"].startswith("standing-"):
                handoff = uuid5(NAMESPACE_URL, f"{self.state.scope}\n{candidate['goal_key']}")
                try:
                    result = self.board.invoke("idle-goal-offer", {
                        "handoff_id": str(handoff), "project": GOAL_PROJECT,
                    }, write=True)
                except SharedActionUnavailable as error:
                    raise SharedReadError(str(error), reason="action-unavailable") from error
                recorded = result.get("offer")
                if (not recorded or recorded.get("task") != candidate["task"]
                        or recorded.get("project") != GOAL_PROJECT or self.lookup() != candidate):
                    return False
            if not can_wake():
                return False
            reserved = self.state.reserve(candidate)
            return reserved

        try:
            outcome = send(offer, claim, record)
        except RuntimeError:
            if reserved:
                return "goal-wake-unknown"
            raise
        if outcome == "already-queued":
            self.state.reserve(candidate)
            self.state.requested(candidate, reminder)
            return "goal-wake-already-queued"
        if outcome == "wake-requested" and reserved:
            self.state.requested(candidate, reminder)
            return "goal-wake-requested"
        return "goal-wake-unknown" if reserved else None


def reminder_title(offer):
    ref = offer["task"]
    url = task_url(offer["board"], {"key": ref, "id": int(ref.removeprefix("T-"))})
    marker = hashlib.sha256(offer["goal_key"].encode()).hexdigest()[:12]
    reason = {
        "deadline": "Goal deadline",
        "daily": "Daily goal review",
        "standing-daily": "Daily standing-goal review",
        "standing-drain": "Standing goal after the Inbox cleared",
    }[offer["goal_kind"]]
    return f"{reason} for {ref}. Check Inbox first, then open {url} and record one concrete outcome. [goal {marker}]"
