"""Select one goal cue and keep its carrier request safe across watcher restarts."""

from contextlib import contextmanager
from datetime import date, datetime
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
_DUE_KINDS = {"deadline", "daily", "standing-daily", "timed"}
_OCCURRENCE_KINDS = {"explore", "review"}


class WakeScheduleStopped(RuntimeError):
    def __init__(self, cue, reason):
        self.cue = cue
        self.reason = reason
        super().__init__(f"Could not schedule the {cue} reminder ({reason}).")


def _ticket(value, project):
    return isinstance(value, str) and re.fullmatch(rf"{re.escape(project)}-[1-9][0-9]{{0,18}}", value) is not None


def default_state_path(board_url, owner, project):
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    folder = Path(base) / "taskboard" / "goal-wakes"
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        raise ValueError("Cannot create the private goal watcher state directory.") from None
    key = hashlib.sha256(f"{board_url}\n{owner}\n{project}".encode()).hexdigest()
    return folder / f"{key}.sqlite"


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

    def __init__(self, path, board_url, owner, *, project=GOAL_PROJECT):
        self.path = Path(path).expanduser().absolute()
        self.project = project
        self.scope = f"{board_url}\n{owner}\n{project}"
        self.working_seen = False
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

    def observe_working(self):
        self.working_seen = True

    def ask_available(self, cue_key):
        with self.transaction() as db:
            return db.execute("SELECT 1 FROM cue WHERE key = ?", ("ask:" + cue_key,)).fetchone() is None

    def reserve_ask(self, offer):
        key = "ask:" + offer["cue_key"]
        with self.transaction() as db:
            return db.execute("INSERT OR IGNORE INTO cue VALUES (?, ?, ?, 0, 'unknown', NULL)",
                              (key, "ask:" + offer["ticket"], offer["remind_on"])).rowcount == 1

    def release_unstarted_ask(self, cue_key):
        with self.transaction() as db:
            db.execute("DELETE FROM cue WHERE key = ? AND outcome = 'unknown'", ("ask:" + cue_key,))

    def record_ask_wake(self, cue_key, reminder):
        with self.transaction() as db:
            db.execute("UPDATE cue SET outcome = 'requested', reminder = ? WHERE key = ?",
                       (reminder, "ask:" + cue_key))

    def select(self, snapshot):
        if snapshot["ordinary_busy"]:
            self.observe_busy()
            return None
        standing = snapshot["standing"]
        continuation = standing.get("continuation_event") if standing else None
        exploring_turn = self.working_seen and standing and standing.get("keep_exploring")
        day = snapshot["day"]
        drain_event = snapshot["drain_event"]
        with self.transaction() as db:
            if "occurrences" in snapshot:
                return self.select_occurrence(db, snapshot, day)
            busy, episode, previous = db.execute(
                "SELECT busy, episode, drain_event FROM handoff WHERE id = 1"
            ).fetchone()
            if not episode or busy or exploring_turn or drain_event > previous:
                episode += 1
                db.execute("UPDATE handoff SET busy = 0, episode = ?, drain_event = ? WHERE id = 1",
                           (episode, max(drain_event, previous)))
            if exploring_turn:
                self.working_seen = False
            for row in snapshot["cues"]:
                offer = _candidate(row, day, episode)
                if self.available(db, offer):
                    return offer
            if not standing or (snapshot["busy"] and continuation is None):
                return None
            ticket = standing["ticket"]
            kind, key = ("standing-drain", f"drain:{ticket}:{episode}")
            if continuation is not None:
                kind, key = ("standing-continuation", f"continue:{ticket}:{continuation}")
            offer = _candidate({**standing, "kind": kind, "key": key}, day, episode)
            return offer if self.available(db, offer) else None

    def select_occurrence(self, db, snapshot, day):
        occurrences = snapshot["occurrences"]
        reviews = [row for row in occurrences if row["kind"] == "review"]
        scheduled = {row["ticket"] for row in reviews}
        # An exact scheduled Review takes the place of the legacy daily cue.
        # A deadline without one and existing exact-time paths still have a carrier.
        fallback = [row for row in snapshot["cues"] if row["kind"] == "timed"
                    or (row["kind"] == "deadline" and row["ticket"] not in scheduled)]
        explore = [row for row in occurrences if row["kind"] == "explore"]
        for row in (*reviews, *fallback, *explore):
            offer = _candidate(row, day, episode=None)
            if self.available(db, offer):
                return offer
        return None

    @staticmethod
    def available(db, offer):
        if db.execute("SELECT 1 FROM cue WHERE key = ?", (offer["goal_key"],)).fetchone():
            return False
        if offer["goal_kind"] == "standing-continuation":
            episode_key = f"drain:{offer['task']}:{offer['goal_episode']}"
            return db.execute("SELECT 1 FROM cue WHERE key = ?", (episode_key,)).fetchone() is None
        if offer["goal_kind"] in _OCCURRENCE_KINDS or offer["goal_kind"] == "timed":
            return True
        previous = db.execute(
            """SELECT MAX(completed_checks) FROM cue WHERE ticket = ? AND day = ?
               AND key NOT LIKE 'explore:%' AND key NOT LIKE 'review:%'""",
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
            if offer["goal_kind"] in ("standing-daily", "standing-continuation"):
                drain = f"drain:{offer['task']}:{offer['goal_episode']}"
                db.execute("INSERT OR IGNORE INTO cue VALUES (?, ?, ?, ?, 'coalesced', NULL)", (
                    drain, offer["task"], offer["goal_day"], offer["completed_checks"],
                ))
        return True

    def release_unstarted(self, offer):
        with self.transaction() as db:
            removed = db.execute("DELETE FROM cue WHERE key = ? AND outcome = 'unknown'",
                                 (offer["goal_key"],)).rowcount
            if removed and offer["goal_kind"] in ("standing-daily", "standing-continuation"):
                drain = f"drain:{offer['task']}:{offer['goal_episode']}"
                db.execute("DELETE FROM cue WHERE key = ? AND day = ? AND outcome = 'coalesced'",
                           (drain, offer["goal_day"]))

    def requested(self, offer, reminder):
        with self.transaction() as db:
            db.execute("UPDATE cue SET outcome = 'requested', reminder = ? WHERE key = ?",
                       (reminder, offer["goal_key"]))

    def reminder(self, offer):
        with self.transaction() as db:
            return db.execute("SELECT reminder FROM cue WHERE key = ?", (offer["goal_key"],)).fetchone()[0]


class GoalWake:
    def __init__(self, board, state, *, include_timed=False, for_wake=False, include_occurrences=False):
        self.board = board
        self.state = state
        self.include_occurrences = include_occurrences
        self.filters = {"project": state.project}
        if include_timed:
            self.filters["include_timed"] = "true"
        if include_occurrences:
            self.filters["include_occurrences"] = "true"
        self.handoff = {"project": state.project}
        if for_wake:
            self.filters["for_wake"] = "true"
            self.handoff["for_wake"] = "true"

    def lookup(self):
        try:
            result = self.board.invoke("my-goal-cues", self.filters)
        except SharedActionUnavailable as error:
            raise SharedReadError(str(error), reason="action-unavailable") from error
        if not self.valid_snapshot(result, self.state.project):
            raise SharedReadError("The board returned an invalid goal cue.", reason="queue-scope")
        if self.include_occurrences:
            if not self.valid_occurrences(result):
                raise SharedReadError("The board returned an invalid goal occurrence.", reason="queue-scope")
            result = {**result, "occurrences": [
                {**item, "completed_checks": 0} for item in result["occurrences"]
            ]}
        return self.state.select(result)

    def valid_occurrences(self, result):
        items = result.get("occurrences")
        if not isinstance(items, list) or (result["ordinary_busy"] and items):
            return False
        seen = set()
        for item in items:
            if not isinstance(item, dict) or not _ticket(item.get("ticket"), self.state.project):
                return False
            kind, key = item.get("kind"), item.get("key")
            if (not isinstance(kind, str) or kind not in _OCCURRENCE_KINDS
                    or not isinstance(key, str) or item.get("day") != result["day"]):
                return False
            first = f"{kind}:{item['ticket']}:{result['day']}"
            continuation = key.removeprefix(first + ":")
            if key != first and not (kind == "explore" and key.startswith(first + ":")
                                     and re.fullmatch(r"[1-9][0-9]{0,18}", continuation)):
                return False
            if key in seen:
                return False
            seen.add(key)
        return True

    @staticmethod
    def valid_snapshot(result, project=GOAL_PROJECT):
        if (not isinstance(result, dict) or type(result.get("busy")) is not bool
                or type(result.get("ordinary_busy")) is not bool or not _day(result.get("day"))
                or (result["ordinary_busy"] and not result["busy"])
                or not _checks(result.get("drain_event")) or not isinstance(result.get("cues"), list)):
            return False
        standing = result.get("standing")
        if standing is not None and (not isinstance(standing, dict) or not _ticket(standing.get("ticket"), project)
                                     or not _checks(standing.get("completed_checks"))):
            return False
        if standing is not None and (
            ("keep_exploring" in standing and type(standing["keep_exploring"]) is not bool)
            or ("continuation_event" in standing and (
                standing.get("keep_exploring") is not True
                or not _checks(standing["continuation_event"]) or not standing["continuation_event"]
            ))
        ):
            return False
        for cue in result["cues"]:
            if (not isinstance(cue, dict) or not _ticket(cue.get("ticket"), project) or cue.get("kind") not in _DUE_KINDS
                    or cue.get("day") != result["day"] or not _checks(cue.get("completed_checks"))):
                return False
            expected_kind = cue["kind"].removeprefix("standing-")
            prefix = f"{expected_kind}:{cue['ticket']}:"
            key = cue.get("key")
            if not isinstance(key, str) or not key.startswith(prefix):
                return False
            day = key.removeprefix(prefix)
            if expected_kind == "timed":
                try:
                    due = datetime.fromisoformat(day)
                except ValueError:
                    return False
                if due.isoformat(timespec="seconds").replace("+00:00", "Z") != day or not day.endswith("Z"):
                    return False
                continue
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
            # Scheduling calls this again only after proving the previous command never launched.
            if reserved:
                self.state.release_unstarted(candidate)
                reserved = False
            if not can_wake() or self.lookup() != candidate:
                return False
            if candidate["goal_kind"].startswith("standing-"):
                handoff = uuid5(NAMESPACE_URL, f"{self.state.scope}\n{candidate['goal_key']}")
                try:
                    result = self.board.invoke("idle-goal-offer", {
                        "handoff_id": str(handoff), **self.handoff,
                    }, write=True)
                except SharedActionUnavailable as error:
                    raise SharedReadError(str(error), reason="action-unavailable") from error
                recorded = result.get("offer")
                if (not recorded or recorded.get("task") != candidate["task"]
                        or recorded.get("project") != self.state.project or self.lookup() != candidate):
                    return False
            if not can_wake():
                return False
            reserved = self.state.reserve(candidate)
            return reserved

        try:
            outcome = send(offer, claim, record)
        except WakeScheduleStopped as error:
            if reserved and error.reason == "carrier-not-started":
                self.state.release_unstarted(candidate)
            raise
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


def reminder_title(offer, *, legacy=False):
    ref = offer["task"]
    url = task_url(offer["board"], {"key": ref, "id": int(ref.rsplit("-", 1)[1])})
    marker = hashlib.sha256(offer["goal_key"].encode()).hexdigest()[:12]
    if offer["goal_kind"] in _OCCURRENCE_KINDS:
        label = "Scheduled goal Review" if offer["goal_kind"] == "review" else "Goal Explore"
        command = f"./tasks --shared goal-complete {ref} {offer['goal_key']} OUTCOME"
        return (f"{label} for {ref}. Check signed Inbox first, then open {url}. "
                f"Record this occurrence with {command}. Recheck Inbox. [goal {marker}]")
    reason = {
        "deadline": "Goal deadline",
        "daily": "Daily goal review",
        "standing-daily": "Daily standing-goal review",
        "timed": "Scheduled goal review",
        "standing-drain": "Standing goal after the Inbox cleared",
        "standing-continuation": "Resume the interrupted standing goal",
    }[offer["goal_kind"]]
    if legacy:
        instructions = f"Check Inbox first, then open {url} and record one concrete outcome."
    else:
        instructions = (f"Check signed Inbox first, then open {url} and record one concrete outcome on the goal. "
                        "Recheck Inbox and follow the goal’s saved mode.")
    return f"{reason} for {ref}. {instructions} [goal {marker}]"
