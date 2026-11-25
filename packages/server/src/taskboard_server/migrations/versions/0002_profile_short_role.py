"""Add the optional short role to existing profiles."""

from alembic import op

revision = "0002_profile_short_role"
down_revision = "0001_profiles_baseline"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE board_principals ADD COLUMN short_role TEXT")
