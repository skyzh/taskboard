# Rehearse the versioned Taskboard schema upgrade

The installed server provides `taskboard-migrate` for disposable rehearsals.
The first Alembic revision adds `board_principals.short_role` to the supported
Taskboard profile layout. The application continues to use its existing SQL.
Start a fresh board with the normal setup workflow before checking its schema.

Restore a verified backup to a separate database. Use the installed server
version and point the command at the restored copy:

```sh
taskboard-migrate --db /path/to/rehearsal.sqlite3 check
taskboard-migrate --db /path/to/rehearsal.sqlite3 upgrade
```

For PostgreSQL, install the server's PostgreSQL extra and set
`TASKS_POSTGRES_DSN` to the restored database in the operator environment.
Run `taskboard-migrate --postgres check` and
`taskboard-migrate --postgres upgrade`.

`check` reports the recognized layout, the recorded revision if present, and
the target. The command validates the tables, writable profile column
definitions, and identity constraints before it records a baseline. If the
supported layout already has `short_role`, the upgrade records the current revision.
For an older supported layout, it adds the column and records the revision in
one transaction. Rerunning the upgrade leaves the schema and records unchanged.
An unsupported layout or a revision mismatch requires operator inspection.

After the rehearsal, start the board on the upgraded copy. Sign in and open an
existing ticket. Compare its title and owner and read the original profile.
Keep the pre-upgrade backup and its matching application build available for
recovery. The first versioned migration is an explicit operator step. Normal
server startup still follows the existing compatibility path.
