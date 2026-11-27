# Upgrade the Taskboard database

Taskboard runs the same ordered Alembic revisions when starting a SQLite or
PostgreSQL server, preparing a PostgreSQL snapshot import, and applying an
operator upgrade. Fresh setup runs the revisions and records the target
revision before accepting requests. Use `taskboard-migrate` from the target
release to check or apply those revisions ahead of startup.

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
definitions, and identity constraints before it records a baseline. The
upgrade applies any pending revision in one transaction. Rerunning it leaves
the schema and records unchanged.
An unsupported layout or a revision mismatch requires operator inspection.
Startup also prepares the existing base tables and applies versioned changes
under the same database lock. If startup fails during a migration, its schema
and revision changes roll back together.

Start the target release on the upgraded copy. Sign in and open an existing
ticket. Compare its title and owner and read the original profile.

Revision `0004_agent_api_usage` creates the daily aggregate table.
Collection stays off until an operator explicitly enables it in the serving
environment. Rehearse the upgrade on a restored copy. Verify the existing
ticket and profile still open, then make an attributed request with fictional
data on the copy. A rollback to a build that only recognizes the earlier
schema uses the pre-upgrade backup.

The rollout of `0005_watched_fyi` is staged. The compatibility release
normally starts at `0004_agent_api_usage`. If it boots as a rollback target
on a ledger already at `0005_watched_fyi`, it serves the existing records
and preserves both acknowledgment tables and the schema revision. Before
enabling the release that writes revision 0005, verify that the mounted
rollback target is compatible and that it can read a signed candidate write
on the same ledger. Other older builds require the pre-upgrade backup.

Ticket-first recurrence uses the tables installed by
`0003_recurring_review_tickets`. A schema upgrade creates those tables if
needed. Adopting a specific schedule is a separate owner action. Existing
attached ticket IDs, schedule IDs, and run records carry forward.

1. On the restored copy, inventory active, paused and canceled ordinary
   schedules, including unbound schedules and schedules in other projects.
   Read `tasks --shared recurring-show R-1` for each owner. The `adoption`
   preview gives the effective project, saved next UTC slot, last recorded
   slot, and open issued tickets visible to the owner. Review those values
   before choosing the next run. Adoption binds an unbound schedule to
   its effective project at the time you adopt it. Leave canceled schedules terminal and handle
   project-managed maintenance through its existing manager controls.
2. Settle issued tickets, pause the chosen ordinary schedule, then run
   `tasks --shared recurring-adopt R-1`. Verify the receipt names one parked
   ticket and a repeated adopt returns the same attachment. Compare the
   original R link and earlier occurrences before resuming.
3. Resume at an explicit UTC slot after the last recorded slot. Check that
   the ticket is parked before the due time, returns to Ready once at the
   due time, and retains its identity and earlier links. Start it, complete
   the review, and verify **Last reviewed** and Activity. Replay the same
   tick and restart before the following slot to verify stable run history.
4. In the rehearsal, stop the scheduler and restore the pre-adoption backup
   to another disposable destination. Verify the prior release against
   that restore. Keep the post-adoption ledger if useful for reconciling
   activity after the backup. Bring the live mapping and next slot to the
   operator for review before converting any live schedule.

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

After enabling reusable recurring work, stop the recurring scheduler before
rolling back. Restore and verify the pre-upgrade database with the old build
before restarting its scheduler.

A SQLite backup restored as SQLite keeps its recorded revision. A SQLite
snapshot import prepares the destination's PostgreSQL schema and copies records
in the same transaction. The original snapshot stays read-only and the
destination gets its own revision record. Run `taskboard-migrate --postgres check`
against the destination and confirm the recorded and target revisions match.
