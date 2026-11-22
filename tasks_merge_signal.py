#!/usr/bin/env python3
"""Launch tasks_merge_signal from this checkout."""

from tools.checkout import add_packages

add_packages("client", "server")

from taskboard_server.merge_signal import main

if __name__ == "__main__":
    raise SystemExit(main())
