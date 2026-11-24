"""Serve the Taskboard API and packaged React application."""

import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    ledger = parser.add_mutually_exclusive_group(required=True)
    ledger.add_argument("--db", type=Path, help="Path to the SQLite ledger.")
    ledger.add_argument("--postgres", action="store_true", help="Use TASKS_POSTGRES_DSN.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--config", type=Path, help="Server TOML with the initial project.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--local-readonly", action="store_true")
    parser.add_argument("--portal-reader", action="store_true")
    parser.add_argument("--websocket-revisions", action="store_true")
    args = parser.parse_args()
    postgres_dsn = os.environ.get("TASKS_POSTGRES_DSN") if args.postgres else None
    if args.postgres and not postgres_dsn:
        parser.error("Set TASKS_POSTGRES_DSN to serve the PostgreSQL ledger.")
    if args.postgres and (args.local_readonly or args.websocket_revisions):
        parser.error("Use PostgreSQL for one shared board with polling.")
    from tasks_service import serve

    return serve(args.db.expanduser().resolve() if args.db else None, args.host, args.port,
                 local_readonly=args.local_readonly, portal_reader=args.portal_reader,
                 websocket_revisions=args.websocket_revisions, postgres_dsn=postgres_dsn,
                 server_config=args.config)
