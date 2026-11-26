"""Run the Taskboard schema migration from a source checkout."""

from checkout import add_packages

add_packages("client", "server")

from taskboard_server.migrate import main


if __name__ == "__main__":
    raise SystemExit(main())
