# Restore and rehearse a Taskboard ledger

Use a private backup directory and a fresh rehearsal destination. Record the
source build, target build, backend, source location, backup time, artifact
digest, and expected records in the operator's upgrade notes. Use fictional
inputs for shared examples. A production backup stays within the operator's
approved storage and access scope.

## SQLite

Use the SQLite backup API to capture one consistent ledger, including data
committed through WAL. Use the same database file the existing service or
launcher uses. Confirm a current restore point at cutover if the source has
changed since rehearsal.

This example creates two new files under a private directory. Set
`taskboard_source_db` to the source path and `taskboard_backup_root` to an
existing private location. The original snapshot stays available while the app
opens the rehearsal copy.

```sh
(
set -eu
umask 077
taskboard_backup_dir="$(mktemp -d "${taskboard_backup_root}/taskboard-upgrade.XXXXXXXX")"
python3 - "$taskboard_source_db" "$taskboard_backup_dir" <<'PY'
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import sys

source = Path(sys.argv[1]).resolve(strict=True)
folder = Path(sys.argv[2]).resolve(strict=True)
backup = folder / "before.sqlite3"
rehearsal = folder / "rehearsal.sqlite3"
with backup.open("xb"):
    pass
with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as live:
    with closing(sqlite3.connect(backup)) as snapshot:
        live.backup(snapshot)
with rehearsal.open("xb") as destination, backup.open("rb") as snapshot:
    shutil.copyfileobj(snapshot, destination)
with closing(sqlite3.connect(rehearsal.as_uri() + "?mode=ro", uri=True)) as restored:
    if restored.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise SystemExit("SQLite rehearsal integrity check failed.")
print(rehearsal)
PY
)
```

For the installed preview, run `python3 tools/install_local.py --root "$taskboard_rehearsal_root"`
from the exact source release with a disposable install directory. Place the rehearsal ledger
at the installer's printed data path before starting the printed launcher.
Read the baseline ticket, run the installer from the exact target with the
same root, and restart the loopback preview. Compare the running preview and
its records with the baseline. For a shared server, select the rehearsal copy
with the normal SQLite `--db` setting and its isolated runtime configuration.
Keep the snapshot closed and available for a separate source-release recovery
rehearsal. [Python SQLite backup API](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup)

## PostgreSQL

Use the database operator's supported backup or snapshot and prove its restore.
For an in-place upgrade with a logical backup, use `pg_dump` and `pg_restore`
from the server's PostgreSQL major version. Check both client versions before
starting. An upgrade of PostgreSQL itself uses the database operator's separate
procedure.
The example assumes an authorized dedicated database and an empty, isolated
database already provisioned for rehearsal. Supply the libpq service names
`taskboard_source_service` and `taskboard_rehearsal_service` in the operator's
environment. Confirm the server, database, and schema reached by each service
and check that the rehearsal destination is isolated and empty before restoring.

```sh
(
set -eu
umask 077
taskboard_backup_dir="$(mktemp -d "${taskboard_backup_root}/taskboard-upgrade.XXXXXXXX")"
pg_dump --dbname="service=$taskboard_source_service" \
  --format=custom --file="$taskboard_backup_dir/before.dump"
pg_restore --list "$taskboard_backup_dir/before.dump" > "$taskboard_backup_dir/before.list"
pg_restore --dbname="service=$taskboard_rehearsal_service" \
  --single-transaction --no-owner --no-privileges "$taskboard_backup_dir/before.dump"
printf '%s\n' "$taskboard_backup_dir/before.dump"
)
```

For a schema-scoped board on a shared database, have the operator restrict the
dump to the authorized schema and include its required dependencies. Match
the board's search path on the rehearsal instance. A disposable restore can
use the fixture role as owner. Have the operator account for production
roles, extensions, and grants in a recovery plan. Preserve the custom backup
even after the rehearsal database is running. [pg_dump](https://www.postgresql.org/docs/16/app-pgdump.html) · [pg_restore](https://www.postgresql.org/docs/16/app-pgrestore.html)
