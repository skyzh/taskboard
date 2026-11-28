"""Run the existing sampled Raft sender for several enrolled scopes in one process."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from fcntl import LOCK_EX, LOCK_NB, flock
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
from tempfile import NamedTemporaryFile
from threading import Event, Lock
from time import monotonic

from raft_auth import Identity
from taskboard_server.managed_ready import ManagedReadySender, SAMPLED_STATE_BY_PROJECT, SampledIdleGate, SenderScope
from taskboard_server.managed_ready_grant import GrantStopped, ManagedReadyGrant, http_json
from taskboard_server.managed_ready_operator import _POLL_SECONDS, _operator_delivery_status, _pilot_status, locked_state
from taskboard_server.ready_wake_operator import _Parser, _read_token, _token_fd, _uuid
from tasks_release_info import release_info
from tasks_storage import has_table, open_sqlite, read


_MAX_SCOPES = 64
_INACTIVE = {"subscription_stopped", "grant_stopped"}
_NON_RUNNING = _INACTIVE | {"starting", "state_locked", "failed"}
_TRANSIENT = (sqlite3.OperationalError, OSError)


def _private_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        os.close(fd)
        raise ValueError("Use an operator-owned private regular file.")
    return fd


@dataclass(frozen=True)
class Enrollment:
    recipient: str
    project: str
    state: Path
    client: str
    secret: Path | None
    goals: bool
    batches: bool

    @property
    def key(self):
        return self.project, self.recipient

    @classmethod
    def from_config(cls, row):
        if not isinstance(row, dict) or row.keys() != {
            "recipient", "project", "state", "client", "secret", "goals", "batches",
        }:
            raise ValueError("Supply the exact enrolled-scope configuration.")
        secret = Path(row["secret"]) if row["secret"] is not None else None
        scope = cls(_uuid(row["recipient"]), row["project"], Path(row["state"]), row["client"],
                    secret, row["goals"], row["batches"])
        if (scope.project not in SAMPLED_STATE_BY_PROJECT or not scope.state.is_absolute()
                or (secret is not None and not secret.is_absolute()) or not isinstance(scope.client, str)
                or not scope.client or type(scope.goals) is not bool or type(scope.batches) is not bool):
            raise ValueError("Use an existing sampled scope and its private state and credential paths.")
        return scope


def _config(path):
    with os.fdopen(_private_file(path), encoding="utf-8") as source:
        data = json.load(source)
    if not isinstance(data, dict) or data.keys() != {"ledger", "server", "slug", "scopes"}:
        raise ValueError("Supply one board, one Raft server, and the approved scopes.")
    slug, rows = data["slug"], data["scopes"]
    if (not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", slug)
            or not isinstance(rows, list) or not 1 <= len(rows) <= _MAX_SCOPES):
        raise ValueError("Supply a valid server slug and a bounded nonempty set of scopes.")
    ledger = Path(data["ledger"]).resolve(strict=True)
    if not ledger.is_file():
        raise ValueError("Use the existing SQLite board ledger.")
    scopes = [Enrollment.from_config(row) for row in rows]
    if len({scope.key for scope in scopes}) != len(scopes):
        raise ValueError("Configure each recipient and project only once.")
    if len({scope.state.resolve() for scope in scopes}) != len(scopes):
        raise ValueError("Configure a distinct saved state for each scope.")
    return ledger, _uuid(data["server"]), slug, scopes


def _observed_at():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _progress(state):
    saved = state.execute("SELECT attempt_event, retry_at FROM sender WHERE singleton = 1").fetchone()
    batch = (state.execute("SELECT retry_at FROM inbox_batch").fetchone()
             if has_table(state, "inbox_batch") else None)
    return {"pending": saved["attempt_event"] is not None or batch is not None,
            "retry_at": max(saved["retry_at"], batch["retry_at"] if batch else 0)}


def _run_scope(ledger, server, slug, enrolled, stop, report, transport, slot_secret):
    identity = Identity(server, enrolled.recipient, "agent", None, None, None)
    scope = SenderScope(ledger, identity, enrolled.client, enrolled.project)
    try:
        with closing(open_sqlite(ledger)) as db, locked_state(enrolled.state, ledger) as state:
            saved = state.execute("SELECT * FROM sender WHERE singleton = 1").fetchone()
            with read(db):
                if saved is None or scope.binding(db) != tuple(saved[key] for key in (
                    "ledger", "board", "server", "recipient", "owner", "app",
                )):
                    raise ValueError("Provide initialized state for the exact enrolled recipient.")
            if saved["stopped"] in _INACTIVE:
                report(saved["stopped"])
                return
            _operator_delivery_status(db, state, scope, resume=False)
            gate = SampledIdleGate(_pilot_status(db, identity, slug), clock=monotonic, include_boundary=True)
            secret = slot_secret if enrolled.secret is None else _read_token(_private_file(enrolled.secret))
            grant = ManagedReadyGrant(enrolled.client, secret, server_slug=slug, identity=identity, transport=transport)
            del secret
            try:
                sender = ManagedReadySender(db, state, scope, grant, idle_gate=gate, sampled=True,
                                            stop_requested=stop.is_set, goal_occurrences=True,
                                            inbox_batches=True)
                report("running", **_progress(state))
                while not stop.is_set():
                    try:
                        outcome, _ = sender.tick()
                        report(outcome, **_progress(state))
                    except _TRANSIENT:
                        gate.reset()
                        report("retrying")
                    else:
                        if outcome in _INACTIVE:
                            return
                    stop.wait(_POLL_SECONDS)
            finally:
                grant.clear()
    except BlockingIOError:
        report("state_locked")
    except GrantStopped:
        report("subscription_stopped")
    except Exception:
        # The private status file records the category, never provider errors or credentials.
        report("failed")


def _write_status(path, data):
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(data, output)
            output.write("\n")
        except BaseException:
            temporary.unlink()
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_slot_credential(scopes, credential):
    clients = {scope.client for scope in scopes if scope.secret is None}
    if not clients and credential is None:
        return None
    if credential is None or clients != {credential[0]}:
        raise ValueError("The selected-slot credential must match its configured app client.")
    return _read_token(_token_fd(credential[1]))


def serve(path, release, ledger, server, slug, scopes, stop, slot_secret, *, transport=http_json):
    manifest = sha256((release / "SHA256SUMS").read_bytes()).hexdigest()
    progress = {scope.key: {"status": "starting"} for scope in scopes}
    lock = Lock()

    def report(scope, status, **details):
        with lock:
            progress[scope.key] = {"status": status, "observed_at": _observed_at(), **details}

    def snapshot():
        with lock:
            rows = [{"project": project, "recipient": recipient, **value}
                    for (project, recipient), value in progress.items()]
        _write_status(path, {
            "pid": os.getpid(), "manifest": manifest, "commit": release_info(release)["commit"],
            "enrolled": len(rows), "active": sum(row["status"] not in _NON_RUNNING for row in rows),
            "scopes": rows,
        })

    snapshot()
    with ThreadPoolExecutor(max_workers=len(scopes)) as pool:
        for scope in scopes:
            pool.submit(_run_scope, ledger, server, slug, scope, stop,
                        lambda status, scope=scope, **details: report(scope, status, **details),
                        transport, slot_secret)
        try:
            while not stop.wait(1):
                snapshot()
        finally:
            stop.set()
    path.unlink(missing_ok=True)


def command(argv):
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--status", required=True, type=Path)
    parser.add_argument("--slot-credential", nargs=2, metavar=("CLIENT", "FD"),
                        help="App binding and private descriptor from the selected serving slot.")
    args = parser.parse_args(argv)
    stop = Event()
    old_signals = {name: signal.signal(name, lambda *_: stop.set()) for name in (signal.SIGINT, signal.SIGTERM)}
    try:
        directory = args.status.parent.stat()
        if directory.st_uid != os.geteuid() or directory.st_mode & 0o077:
            raise ValueError("Use a private status directory owned by the operator.")
        ledger, server, slug, scopes = _config(args.config)
        slot_secret = _read_slot_credential(scopes, args.slot_credential)
        lock_path = args.status.with_name(args.status.name + ".lock")
        with os.fdopen(os.open(lock_path, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600), "rb") as lock:
            flock(lock, LOCK_EX | LOCK_NB)
            serve(args.status, Path(__file__).resolve().parents[4], ledger, server, slug, scopes, stop, slot_secret)
    except (OSError, ValueError, TypeError, AttributeError):
        parser.exit(1, "Managed notification service could not start.\n")
    finally:
        for name, handler in old_signals.items():
            signal.signal(name, handler)
    return 0
