"""Submit one approved Ready wake from the Taskboard operator environment."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import stat
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener
from uuid import UUID

from raft_auth import Identity
from taskboard_client.references import MAX_ID, identifier, prefix
from taskboard_server.ready_wake import WakeRecipient, launch_ready_wake
from tasks_storage import Postgres, open_sqlite


_MAX_TOKEN_BYTES = 16 * 1024
_MAX_RESPONSE_BYTES = 64 * 1024
_TIMEOUT_SECONDS = 5


class _Parser(argparse.ArgumentParser):
    def error(self, _message):
        self.exit(2, "Invalid Ready wake arguments. Run with --help.\n")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _positive_id(value):
    if (not value.isascii() or not value.isdecimal() or value.startswith("0")
            or len(value) > 19 or int(value) > MAX_ID):
        raise ValueError("Expected a positive numbered ID.")
    return int(value)


def _token_fd(value):
    number = _positive_id(value)
    if not 3 <= number < 2**31:
        raise ValueError("Expected a private descriptor above 2.")
    return number


def _project(value):
    if prefix(value) != value:
        raise ValueError("Expected a canonical project prefix.")
    return value


def _task(value):
    project, number = identifier(value)
    if value != f"{project}-{number}":
        raise ValueError("Expected a canonical ticket reference.")
    return value


def _uuid(value):
    if str(UUID(value)) != value:
        raise ValueError("Expected a canonical Raft UUID.")
    return value


def _read_token(fd):
    with os.fdopen(fd, "rb") as handle:
        source = os.fstat(handle.fileno())
        if not stat.S_ISREG(source.st_mode) or source.st_uid != os.geteuid() or source.st_mode & 0o077:
            raise ValueError("Expected an operator-owned private regular file.")
        raw = handle.read(_MAX_TOKEN_BYTES + 1)
    if not 0 < len(raw) <= _MAX_TOKEN_BYTES:
        raise ValueError("Expected a bounded token file.")
    return (raw[:-2] if raw.endswith(b"\r\n") else raw.removesuffix(b"\n")).decode("ascii")


def _post(request):
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        response = opener.open(request, timeout=_TIMEOUT_SECONDS)
    except HTTPError as error:
        with closing(error):
            return error.code, None
    with response:
        status = response.getcode()
        if status not in (200, 202):
            return status, None
        raw = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        return status, None
    return status, json.loads(raw)


def _report(status, exit_code, *, reason=None):
    result = {"status": status}
    if reason:
        result["reason"] = reason
    print(json.dumps(result, sort_keys=True))
    return exit_code


def main(argv=None, *, connect=Postgres, transport=_post):
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--db", action="append", default=[], type=Path,
                        help="Path to the existing SQLite ledger. Without it, use TASKS_POSTGRES_DSN.")
    parser.add_argument("--project", required=True, type=_project, help="Exact project prefix.")
    parser.add_argument("--task", required=True, type=_task, help="Canonical ticket reference.")
    parser.add_argument("--event-id", required=True, type=_positive_id, help="Exact Ready event number.")
    parser.add_argument("--server-id", required=True, type=_uuid, help="Approved Raft Server UUID.")
    parser.add_argument("--recipient-id", required=True, type=_uuid, help="Approved Raft agent UUID.")
    parser.add_argument("--token-fd", required=True, type=_token_fd,
                        help="Private file descriptor containing the approved recipient's agent-events token.")
    args = parser.parse_args(argv)
    dsn = os.environ.get("TASKS_POSTGRES_DSN")
    if len(args.db) > 1 or (args.db and dsn):
        return _report("not_attempted", 2, reason="ambiguous_ledger_configuration")
    if not args.db and not dsn:
        return _report("not_attempted", 2, reason="missing_ledger_configuration")
    attempted = False

    def submit(request):
        nonlocal attempted
        attempted = True
        return transport(request)

    try:
        try:
            token = _read_token(args.token_fd)
        except (OSError, ValueError):
            return _report("not_attempted", 2, reason="invalid_token_file")
        recipient = WakeRecipient(Identity(args.server_id, args.recipient_id, "agent", None, None, None), token)
        if args.db:
            path = args.db[0].expanduser().resolve(strict=True)
            if not path.is_file():
                raise ValueError("Select an existing SQLite ledger file.")
            db = open_sqlite(path)
        else:
            db = connect(dsn)
        with closing(db):
            status = launch_ready_wake(db, project=args.project, task=args.task, event_id=args.event_id,
                                       recipient=recipient, transport=submit)
    except KeyboardInterrupt:
        return _report("unknown" if attempted else "not_attempted", 130, reason="interrupted")
    except Exception:
        # Storage and transport errors can contain connection strings or requests.
        if attempted:
            return _report("unknown", 1, reason="attempt_or_receipt_failed")
        return _report("not_attempted", 1, reason="scope_or_ledger_check_failed")
    return _report(status, int(status not in ("accepted", "already_accepted")))
