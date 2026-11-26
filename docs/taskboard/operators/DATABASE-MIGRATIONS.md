# Upgrade the Taskboard database

Use `taskboard-migrate` from the target server release to check and upgrade an
existing ledger before starting that release.
The first Alembic revision adds `board_principals.short_role` to the supported
Taskboard profile layout. The application continues to use its existing SQL.
Start a fresh board with the normal setup workflow before checking its schema.

## Rehearse on a restored backup

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

From a source checkout, run the same commands as
`uv run --frozen python tools/migrate_database.py --db /path/to/ledger.sqlite3 check`
or `uv run --frozen --extra postgres python tools/migrate_database.py --postgres check`.
Replace `check` with `upgrade` to apply the revision.

`check` reports the recognized layout, the recorded revision if present, and
the target. The command validates the tables, writable profile column
definitions, and identity constraints before it records a baseline. If the
supported layout already has `short_role`, the upgrade records the current revision.
For an older supported layout, it adds the column and records the revision in
one transaction. Rerunning the upgrade leaves the schema and records unchanged.
An unsupported layout or a revision mismatch requires operator inspection.

Start the target release on the upgraded copy. Sign in and open an existing
ticket. Compare its title and owner and read the original profile.

## Apply the checked upgrade

1. Stop Taskboard and any background writers using that ledger. Keep the prior
   application build and a fresh verified backup for recovery.
2. Confirm the target release command points to the same database and schema
   as the service. Run `check`, then `upgrade`. Run `check` again and confirm
   the recorded and target revisions match.
3. Start the target release using the normal service command. Sign in and open
   an existing ticket to confirm the profile and saved data are available.

If a migration fails, inspect the reported error before starting the target
release. If the service fails after a successful migration, restore the backup
to a separate database, verify the prior release against that copy, then switch
the service to the verified restore.

This release still accepts ledgers upgraded by the existing startup procedure.
A SQLite backup restored as SQLite keeps its revision. When importing a SQLite
snapshot into PostgreSQL, run the versioned check and upgrade against the
destination after import. Its revision is recorded from the PostgreSQL schema.
