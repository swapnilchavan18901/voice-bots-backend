"""Scope calendar inbox and series preferences to the event organizer."""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("calendar_email", sa.String(320)))
    op.create_index("uq_users_calendar_email", "users", ["calendar_email"], unique=True)
    op.add_column("meeting_calendar_events", sa.Column("organizer_email", sa.String(320)))
    op.create_index(
        "ix_meeting_calendar_events_organizer_email", "meeting_calendar_events", ["organizer_email"]
    )
    op.add_column("meeting_series", sa.Column("organizer_email", sa.String(320)))


def downgrade():
    op.drop_index("uq_users_calendar_email", table_name="users")
    op.drop_column("users", "calendar_email")
    op.drop_column("meeting_series", "organizer_email")
    op.drop_index(
        "ix_meeting_calendar_events_organizer_email", table_name="meeting_calendar_events"
    )
    op.drop_column("meeting_calendar_events", "organizer_email")
