"""Initialize or run one explicitly authorized managed Ready sender."""

from contextlib import closing, contextmanager
from fcntl import LOCK_EX, LOCK_NB, flock
import json
import os
import re
from pathlib import Path
import signal
import stat
import subprocess
import sys
from threading import Event
from time import monotonic

from raft_auth import Identity
from taskboard_server.managed_ready import SAMPLED_STATE_BY_PROJECT, ManagedReadySender, SampledIdleGate, SenderScope, initialize
from taskboard_server.managed_ready_grant import GrantStopped, ManagedReadyGrant, http_json
from taskboard_server.ready_command import ReadyCommand
from taskboard_server.ready_signal import connect
from taskboard_server.ready_wake_operator import _Parser, _positive_id, _read_token, _token_fd, _uuid
import tasks_app_wake_activity as app_wake_activity
from tasks_profiles import for_identity
import tasks_ring1 as ring1
from tasks_storage import open_sqlite, read, write
from tasks_wake_subscriptions import MANAGED_READY_PROJECT


_POLL_SECONDS = 10
_PILOT_SECONDS = 5 * 60
_PILOT_RETRY = {"awaiting_idle", "waiting", "retry", "renewal_needed"}
_PILOT_ACCEPTED = {"accepted", "already_accepted"}
_DELIVERY_COMMANDS = {"delivery-status", "resume-delivery"}


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
        if isinstance(event, str):
            key = "inbox_batch" if event.startswith("taskboard-inbox-") else "goal_occurrence"
        else:
            key = "ready_event"
        record[key] = event
    print(json.dumps(record), flush=True)


def _run(sender, stop):
    _emit("running")
    previous = None
    while not stop.is_set():
        outcome, event = sender.tick()
        if outcome not in ("idle", "waiting") and (outcome != "delivery_paused" or outcome != previous):
            _emit(outcome, event)
        previous = outcome
        if outcome in ("subscription_stopped", "grant_stopped"):
            return 0
        stop.wait(_POLL_SECONDS)
    _emit("stopped")
    return 0


def _raft(*args):
    return subprocess.run(
        ["raft", *args], check=True, capture_output=True, text=True, timeout=5,
    ).stdout


def _pilot_status(db, identity, server_slug):
    principal = for_identity(db, identity)
    handle = principal["username"] if principal else ""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", handle):
        raise ValueError("The enrolled recipient needs a verified Raft handle.")
    auth = json.loads(_raft("auth", "whoami"))["data"]
    server = auth["server"]
    if not auth["serverConfirmed"] or (server["id"], server["slug"]) != (identity.server_id, server_slug):
        raise ValueError("Raft must resolve the enrolled server.")

    def read_status():
        try:
            text = _raft("user", "info", f"@{handle}", "--limit", "1")
        except subprocess.SubprocessError:
            return "unknown"
        header = re.split(r"\n(?:Description |### )", text, maxsplit=1)[0].splitlines()
        users = [line.casefold() for line in header if line.startswith("User: ")]
        kinds = [line for line in header if line.startswith("Kind: ")]
        statuses = [line for line in header if line.startswith("Status: ")]
        if users == [f"user: @{handle}".casefold()] and kinds == ["Kind: agent"] and statuses == [
            "Status: active; online: Idle",
        ]:
            return "idle"
        return "unknown"

    return read_status


def _check_run_mode(state, event_id, *, sampled=False, project=MANAGED_READY_PROJECT):
    gated_state = state.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pilot'",
    ).fetchone()
    if (gated_state is not None) != (sampled or event_id is not None):
        raise ValueError("Use state initialized for this managed sender mode.")
    if gated_state is None:
        return
    with state:
        (pinned,) = state.execute("SELECT event FROM pilot").fetchone()
        if sampled:
            if pinned != SAMPLED_STATE_BY_PROJECT[project]:
                raise ValueError("Use state initialized for this sampled sender project.")
            return
        if pinned is None:
            state.execute("UPDATE pilot SET event = ?", (event_id,))
        elif pinned != event_id:
            raise ValueError("The pilot state is pinned to another event.")


def _operator_delivery_status(db, state, scope, *, resume):
    _check_run_mode(state, None, sampled=True, project=scope.project)
    saved = state.execute("SELECT * FROM sender WHERE singleton = 1").fetchone()
    with (write(db) if resume else read(db)):
        if ring1.private(db, scope.project) or scope.subscription(db) != tuple(saved[key] for key in (
            "ledger", "board", "server", "recipient", "owner", "app", "subscription",
        )):
            raise ValueError("Use the current approved sender scope.")
        owner = saved["owner"]
        if resume:
            app_wake_activity.operator_resume(db, owner, scope.project, app_wake_activity.observation_at())
        return app_wake_activity.status(db, owner, scope.project)


def _run_pilot(sender, stop, event_id):
    _emit("pilot_running", event_id)
    deadline = monotonic() + _PILOT_SECONDS
    while not stop.is_set() and monotonic() < deadline:
        outcome, event = sender.tick()
        if outcome not in ("awaiting_idle", "waiting"):
            _emit(outcome, event or event_id)
        if outcome not in _PILOT_RETRY:
            return 0 if outcome in _PILOT_ACCEPTED else 1
        stop.wait(_POLL_SECONDS)
    _emit("pilot_stopped" if stop.is_set() else "pilot_window_ended", event_id)
    return 1


def main(argv=None, *, transport=http_json):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "run-service":
        from taskboard_server.managed_ready_service import command

        return command(argv[1:])
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--db", required=True, type=Path, help="Existing writable SQLite board ledger.")
    parser.add_argument("--state", required=True, type=Path, help="Separate private managed sender state.")
    parser.add_argument("--server-id", required=True, type=_uuid, help="Approved Raft Server UUID.")
    parser.add_argument("--recipient-id", required=True, type=_uuid, help="Approved Raft agent UUID.")
    parser.add_argument("--project", default=MANAGED_READY_PROJECT, choices=tuple(SAMPLED_STATE_BY_PROJECT),
                        help="Enroll one project. Tiny LLM requires direct sampled Raft delivery.")
    transport_options = parser.add_mutually_exclusive_group(required=True)
    transport_options.add_argument("--client-id", help="Public OAuth app client identifier for direct Raft delivery.")
    transport_options.add_argument("--delivery-command", type=ReadyCommand.parse,
                                   help="Trusted local JSON argv array for sampled delivery.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create a fresh baseline after the recipient opts in.")
    commands.add_parser("init-pilot", help="Create a private baseline for the one-event pilot.")
    commands.add_parser("init-sampled", help="Create private state for recurring observed-Idle delivery.")
    commands.add_parser("delivery-status", help="Read the approved sampled app sender's pause status.")
    commands.add_parser("resume-delivery", help="Resume paused app delivery as the authorized local operator.")
    run = commands.add_parser("run", help="Run in the foreground as the authorized operator.")
    sampled_run = commands.add_parser("run-sampled", help="Send pending Ready cues after observed Idle.")
    sampled_run.add_argument("--goal-occurrences", action="store_true",
                             help="Also send signed, opted-in Explore and scheduled Review cues through Raft.")
    sampled_run.add_argument("--inbox-batches", action="store_true",
                             help="Send one direct Raft app event for an enrolled Inbox batch.")
    pilot = commands.add_parser("run-pilot", help="Try one enrolled Ready event in a bounded foreground window.")
    pilot.add_argument("--event-id", required=True, type=_positive_id, help="Exact enrolled Ready event number.")
    for command in (run, pilot, sampled_run):
        command.add_argument("--server-slug", required=True, help="Approved Raft Server slug.")
        command.add_argument("--secret-fd", type=_token_fd,
                             help="Private descriptor for the OAuth app secret in direct Raft delivery.")
    args = parser.parse_args(argv)
    goal_occurrences = getattr(args, "goal_occurrences", False)
    inbox_batches = getattr(args, "inbox_batches", False)
    if args.project != MANAGED_READY_PROJECT and (args.command not in {"init-sampled", "run-sampled", *_DELIVERY_COMMANDS}
                                                  or args.delivery_command is not None):
        parser.error("Tiny LLM requires direct sampled Raft delivery.")
    if args.delivery_command is not None and (args.command not in ("init-sampled", "run-sampled")
                                              or getattr(args, "secret_fd", None) is not None
                                              or goal_occurrences or inbox_batches):
        parser.error("Use command delivery with sampled mode and no OAuth secret.")
    if args.delivery_command is None and args.command.startswith("run") and args.secret_fd is None:
        parser.error("Supply the private OAuth secret descriptor for direct Raft delivery.")

    try:
        ledger = args.db.expanduser().resolve(strict=True)
        if not ledger.is_file():
            raise ValueError("Use an existing SQLite ledger file.")
        state_path = args.state.expanduser().absolute()
        identity = Identity(args.server_id, args.recipient_id, "agent", None, None, None)
        binding = args.delivery_command.binding if args.delivery_command is not None else args.client_id
        scope = SenderScope(ledger, identity, binding, args.project)
        with closing(open_sqlite(ledger)) as db:
            if args.command in ("init", "init-pilot", "init-sampled"):
                _emit("initialized", initialize(state_path, db, scope, pilot=args.command == "init-pilot",
                                                sampled=args.command == "init-sampled"))
                return 0
            with locked_state(state_path, ledger) as state:
                if args.command in _DELIVERY_COMMANDS:
                    print(json.dumps(_operator_delivery_status(db, state, scope, resume=args.command == "resume-delivery")),
                          flush=True)
                    return 0
                event_id = getattr(args, "event_id", None)
                sampled = args.command == "run-sampled"
                _check_run_mode(state, event_id, sampled=sampled, project=scope.project)
                idle_gate = None
                if sampled or event_id is not None:
                    idle_gate = SampledIdleGate(_pilot_status(db, identity, args.server_slug), clock=monotonic,
                                                include_boundary=sampled)
                grant = None
                if args.delivery_command is None:
                    grant = ManagedReadyGrant(args.client_id, _read_token(args.secret_fd),
                                              server_slug=args.server_slug, identity=identity, transport=transport)
                stop = Event()
                old_term = signal.signal(signal.SIGTERM, lambda *_: stop.set())
                old_int = signal.signal(signal.SIGINT, lambda *_: stop.set())
                try:
                    sender = ManagedReadySender(db, state, scope, grant, idle_gate=idle_gate, event_id=event_id,
                                                sampled=sampled, stop_requested=stop.is_set,
                                                command_delivery=args.delivery_command, goal_occurrences=goal_occurrences,
                                                inbox_batches=inbox_batches)
                    if event_id is not None:
                        return _run_pilot(sender, stop, event_id)
                    return _run(sender, stop)
                finally:
                    if grant is not None:
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
