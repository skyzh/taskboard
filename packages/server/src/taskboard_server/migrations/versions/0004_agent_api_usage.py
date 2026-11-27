"""Store daily signed agent API request diagnostics."""

from alembic import op

from tasks_api_usage import SCHEMA

revision = "0004_agent_api_usage"
down_revision = "0003_recurring_review_tickets"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(SCHEMA)


def downgrade():
    raise RuntimeError("Restore a pre-upgrade backup to return to the previous schema.")
