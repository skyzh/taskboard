#!/usr/bin/env python3
"""Launch taskboard_cli.ready_checkpoint from this checkout."""

from tools.checkout import add_packages

add_packages("client", "cli")

from taskboard_cli.ready_checkpoint import main

if __name__ == "__main__":
    raise SystemExit(main())
