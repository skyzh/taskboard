#!/usr/bin/env python3
"""Launch tasks_ready_signal from this checkout."""

from tools.checkout import add_packages

add_packages("client", "server")

from taskboard_server.ready_signal import main

if __name__ == "__main__":
    raise SystemExit(main())
