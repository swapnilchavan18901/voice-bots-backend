"""Clinic inbound number routing and durable outbound call attempts."""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "clinic_phone_routes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "bot_id",
            sa.Uuid(),
            sa.ForeignKey("bots.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("phone_number", sa.String(20), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("token_version", sa.Uuid(), nullable=False),
    )
    op.create_table(
        "clinic_phone_calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "bot_id",
            sa.Uuid(),
            sa.ForeignKey("bots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_id", sa.Uuid()),
        sa.Column("direction", sa.String(20), nullable=False),
        sa.Column("phone_number", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("provider_call_sid", sa.String(128), unique=True),
        sa.Column("stream_claimed", sa.Boolean(), nullable=False),
        sa.Column("error", sa.String(255)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("bot_id", "request_id"),
        sa.CheckConstraint("direction IN ('inbound', 'outbound')"),
        sa.CheckConstraint(
            "status IN ('dialing', 'unknown', 'queued', 'ringing', 'in-progress', 'completed', 'failed', 'busy', 'no-answer', 'cancelled')"
        ),
    )
    op.create_index("ix_clinic_phone_calls_bot_id", "clinic_phone_calls", ["bot_id"])


def downgrade():
    op.drop_table("clinic_phone_calls")
    op.drop_table("clinic_phone_routes")
