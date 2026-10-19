"""Persist Attendee voice sessions, transcripts, and notes."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "meeting_captures",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("meeting_id", sa.Uuid(), nullable=False),
        sa.Column("provider_bot_id", sa.String(100), nullable=True, unique=True),
        sa.Column("speaker_names", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("provider_status", sa.String(100), nullable=True),
        sa.Column("transcript", sa.JSON(), nullable=False),
        sa.Column("transcript_status", sa.String(20), nullable=False),
        sa.Column("notes", sa.JSON(), nullable=True),
        sa.Column("notes_status", sa.String(20), nullable=False),
        sa.Column("notes_error", sa.Text(), nullable=True),
        sa.Column("notes_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "meeting_id"],
            ["meetings.organization_id", "meetings.id"],
            name="fk_meeting_capture_owner",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("meeting_id", name="uq_meeting_capture_meeting"),
    )
    op.create_index("ix_meeting_captures_organization_id", "meeting_captures", ["organization_id"])
    op.create_index("ix_meeting_captures_meeting_id", "meeting_captures", ["meeting_id"])


def downgrade():
    op.drop_table("meeting_captures")
