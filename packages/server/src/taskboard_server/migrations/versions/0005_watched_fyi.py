"""Store opt-in watched update acknowledgments."""

from alembic import op

revision = "0005_watched_fyi"
down_revision = "0004_agent_api_usage"
branch_labels = None
depends_on = None

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS watched_fyi_preferences (
        principal TEXT NOT NULL PRIMARY KEY REFERENCES board_principals (ledger_key)
    )""",
    """CREATE TABLE IF NOT EXISTS watched_fyi_receipts (
        recipient TEXT NOT NULL REFERENCES board_principals (ledger_key),
        event_id BIGINT NOT NULL REFERENCES events (id),
        acknowledged_at TEXT NOT NULL,
        PRIMARY KEY (recipient, event_id)
    )""",
)


def upgrade():
    for statement in SCHEMA:
        op.execute(statement)


def downgrade():
    raise RuntimeError("Restore a pre-upgrade backup to return to the previous schema.")
