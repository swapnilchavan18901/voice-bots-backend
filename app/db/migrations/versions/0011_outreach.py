"""Organization-scoped Audiva Outreach agents, campaigns and persistent call queue."""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def identity():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade():
    op.create_table(
        "outreach_agents",
        *identity(),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("sales_context", sa.Text(), nullable=False),
        sa.Column("language", sa.String(20), nullable=False),
    )
    op.create_index(
        "ix_outreach_agents_organization_id", "outreach_agents", ["organization_id"]
    )
    op.create_table(
        "outreach_campaigns",
        *identity(),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_id", sa.Uuid(), sa.ForeignKey("outreach_agents.id"), nullable=False
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("agent_snapshot", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("mode IN ('sequential', 'parallel')"),
        sa.CheckConstraint(
            "status IN ('draft', 'running', 'stopping', 'completed', 'cancelled')"
        ),
    )
    op.create_index(
        "ix_outreach_campaigns_organization_id",
        "outreach_campaigns",
        ["organization_id"],
    )
    op.create_index("ix_outreach_campaigns_status", "outreach_campaigns", ["status"])
    op.create_table(
        "outreach_calls",
        *identity(),
        sa.Column(
            "campaign_id",
            sa.Uuid(),
            sa.ForeignKey("outreach_campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("phone_number", sa.String(20), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("provider_call_sid", sa.String(128), unique=True),
        sa.Column("stream_claimed", sa.Boolean(), nullable=False),
        sa.Column("error", sa.String(255)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("campaign_id", "position"),
        sa.UniqueConstraint("campaign_id", "phone_number"),
        sa.CheckConstraint(
            "status IN ('pending', 'dialing', 'unknown', 'queued', 'ringing', 'in-progress', 'completed', 'failed', 'busy', 'no-answer', 'cancelled')"
        ),
    )
    op.create_index(
        "ix_outreach_calls_campaign_status", "outreach_calls", ["campaign_id", "status"]
    )


def downgrade():
    op.drop_table("outreach_calls")
    op.drop_table("outreach_campaigns")
    op.drop_table("outreach_agents")
