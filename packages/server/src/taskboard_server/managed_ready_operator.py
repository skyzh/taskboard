"""Initialize or run one explicitly authorized managed Ready sender."""

from contextlib import closing, contextmanager
from fcntl import LOCK_EX, LOCK_NB, flock
import json
import os
from pathlib import Path
import signal
import stat
from threading import Event

from raft_auth import Identity
from taskboard_server.managed_ready import ManagedReadySender, SenderScope, initialize
from taskboard_server.managed_ready_grant import GrantStopped, ManagedReadyGrant, http_json
from taskboard_server.ready_signal import connect
from taskboard_server.ready_wake_operator import _Parser, _read_token, _token_fd, _uuid
from tasks_storage import open_sqlite


_POLL_SECONDS = 10


@contextmanager
def locked_state(path, ledger_path):
    source = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(source.st_mode) or source.st_uid != os.geteuid() or source.st_mode & 0o077:
        raise ValueError("Use a private sender state owned by the operator.")
    if os.path.samestat(source, ledger_path.stat()):
        raise ValueError("Sender state must be separate from the board ledger.")
    # Keep process locking off the SQLite file so its own locks still work.
    lock = path.with_name(path.name + ".lock")
    flags = os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(lock, flags, 0o600), "rb") as handle:
        lock_stat = os.fstat(handle.fileno())
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != os.geteuid() or lock_stat.st_mode & 0o077:
            raise ValueError("Use a private sender lock owned by the operator.")
        flock(handle.fileno(), LOCK_EX | LOCK_NB)
        with closing(connect(path, "rw")) as state:
            yield state


def _emit(status, event=None):
    record = {"status": status}
    if event is not None:
        record["ready_event"] = event
    print(json.dumps(record), flush=True)


def _run(sender, stop):
    _emit("running")
    while not stop.is_set():
        outcome, event = sender.tick()
        if outcome not in ("idle", "waiting"):
            _emit(outcome, event)
        if outcome in ("subscription_stopped", "grant_stopped"):
            return 0
        stop.wait(_POLL_SECONDS)
    _emit("stopped")
    return 0


def main(argv=None, *, transport=http_json):
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--db", required=True, type=Path, help="Existing writable SQLite board ledger.")
    parser.add_argument("--state", required=True, type=Path, help="Separate private managed sender state.")
    parser.add_argument("--server-id", required=True, type=_uuid, help="Approved Raft Server UUID.")
    parser.add_argument("--recipient-id", required=True, type=_uuid, help="Approved Raft agent UUID.")
    parser.add_argument("--client-id", required=True, help="Public OAuth app client identifier.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create a fresh baseline after the recipient opts in.")
    run = commands.add_parser("run", help="Run in the foreground as the authorized operator.")
    run.add_argument("--server-slug", required=True, help="Approved Raft Server slug.")
    run.add_argument("--secret-fd", required=True, type=_token_fd,
                     help="Private file descriptor containing the OAuth app secret.")
    args = parser.parse_args(argv)

    try:
        ledger = args.db.expanduser().resolve(strict=True)
        if not ledger.is_file():
            raise ValueError("Use an existing SQLite ledger file.")
        state_path = args.state.expanduser().absolute()
        identity = Identity(args.server_id, args.recipient_id, "agent", None, None, None)
        scope = SenderScope(ledger, identity, args.client_id)
        with closing(open_sqlite(ledger)) as db:
            if args.command == "init":
                _emit("initialized", initialize(state_path, db, scope))
                return 0
            with locked_state(state_path, ledger) as state:
                grant = ManagedReadyGrant(args.client_id, _read_token(args.secret_fd),
                                          server_slug=args.server_slug, identity=identity, transport=transport)
                stop = Event()
                old_term = signal.signal(signal.SIGTERM, lambda *_: stop.set())
                old_int = signal.signal(signal.SIGINT, lambda *_: stop.set())
                try:
                    return _run(ManagedReadySender(db, state, scope, grant), stop)
                finally:
                    grant.clear()
                    signal.signal(signal.SIGTERM, old_term)
                    signal.signal(signal.SIGINT, old_int)
    except GrantStopped:
        _emit("subscription_stopped")
        return 1
    except Exception:
        # Configuration and upstream exceptions can contain credentials.
        _emit("managed_sender_failed")
        return 1
