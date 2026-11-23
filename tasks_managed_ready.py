#!/usr/bin/env python3
"""Launch the managed Ready operator command from this checkout."""

from tools.checkout import add_packages

add_packages("client", "server")

from taskboard_server.managed_ready_operator import main

if __name__ == "__main__":
    raise SystemExit(main())
