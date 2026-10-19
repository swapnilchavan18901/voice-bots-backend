"""Remove calendar fields unused by bounded snapshot sync."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column("meeting_calendars", "sync_token")
    op.drop_column("meeting_calendar_events", "provider_updated_at")


def downgrade():
    op.add_column("meeting_calendars", sa.Column("sync_token", sa.Text(), nullable=True))
    op.add_column(
        "meeting_calendar_events",
        sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
