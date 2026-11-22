"""Record private and operator mutations without storing their values."""

SCHEMA = """
    CREATE TABLE IF NOT EXISTS audit_events (
        id INTEGER PRIMARY KEY,
        scope TEXT NOT NULL CHECK (scope IN ('private', 'operator')),
        actor TEXT,
        operation TEXT NOT NULL,
        record_type TEXT NOT NULL,
        record_id TEXT NOT NULL,
        at TEXT NOT NULL
    )
"""


def record(db, *, scope, actor, operation, record_type, record_id, at):
    """Append an event in the caller's transaction without committing it."""
    if not db.in_transaction:
        db.execute("BEGIN")
    db.execute(SCHEMA)
    db.execute(
        """
        INSERT INTO audit_events (scope, actor, operation, record_type, record_id, at)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (scope, actor, operation, record_type, record_id, at),
    )
