"""Command line entry point for Taskboard."""

import sys

from taskboard_client.work import ACTIVITY_TECHNICAL, STRUCTURED_ACTIVITY, activity_details, is_activity_headline

from .arguments import arguments
from .shared import shared_command


def main(argv=None):
    args = arguments(argv)
    try:
        if args.command == "park" and (args.review_on is None) != (args.review_criteria is None):
            if args.review_on is None:
                raise ValueError("--review-criteria requires --review-on.")
            raise ValueError("--review-on requires --review-criteria.")
        if args.command in STRUCTURED_ACTIVITY:
            if args.details is not None or getattr(args, "checkpoint", False):
                args.details = activity_details(args.note, args.details or "")
            elif is_activity_headline(args.note) and not ACTIVITY_TECHNICAL.search(args.note):
                # Keep long or technical note-only input compatible with existing commands.
                args.details = ""
        if args.mode == "shared":
            print(shared_command(args))
            return 0
        if args.command == "assign-human" and args.on is None:
            raise ValueError("Supply the decision recipient with --on.")
        if args.command == "raise-attention":
            raise ValueError("Attention requests use the shared board. Run `./tasks --shared raise-attention --help`.")
        if args.command == "search":
            raise ValueError("Search uses the shared board. Run `./tasks --shared search QUERY`.")
        if args.command == "confirm-blocker":
            raise ValueError("Blocker confirmation uses the shared board. Run `./tasks --shared confirm-blocker --help`.")
        try:
            from tasks_core import run_local
        except ModuleNotFoundError as error:
            if error.name != "tasks_core":
                raise
            raise RuntimeError("Install taskboard-cli[offline] for local ledger and server commands.") from error
        return run_local(args)
    except (LookupError, ValueError, OSError, OverflowError, RuntimeError) as error:
        print(f"tasks: {error}", file=sys.stderr)
        return 1
