#!/bin/sh
set -eu
umask 077

root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
IFS= read -r release < "$root/current"
app="$root/releases/$release"
ledger="$root/data/taskboard.sqlite3"

case "${1:-}" in
    serve)
        shift
        exec "$app/.venv/bin/python" "$app/tasks" --offline --db "$ledger" serve "$@" \
            --local-readonly --host 127.0.0.1
        ;;
    --help|-h|'')
        exec "$app/.venv/bin/python" "$app/tasks" --help
        ;;
    -*)
        echo 'Place the local Taskboard command first, for example taskboard show T-1.' >&2
        exit 2
        ;;
    *)
        exec "$app/.venv/bin/python" "$app/tasks" --offline --db "$ledger" "$@"
        ;;
esac
