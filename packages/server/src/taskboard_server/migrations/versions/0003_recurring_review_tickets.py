"""Store reusable recurring ticket associations and completed review outcomes."""

from alembic import op

from tasks_recurring import REVIEW_SCHEMA

revision = "0003_recurring_review_tickets"
down_revision = "0002_profile_short_role"
branch_labels = None
depends_on = None


def upgrade():
    for statement in REVIEW_SCHEMA:
        op.execute(statement)


def downgrade():
    raise RuntimeError("Restore a pre-upgrade backup to return to the previous recurring-work schema.")
