#!/usr/bin/env python3
"""Launch the Ready wake operator command from this checkout."""

from tools.checkout import add_packages

add_packages("client", "server")

from taskboard_server.ready_wake_operator import main

if __name__ == "__main__":
    raise SystemExit(main())
