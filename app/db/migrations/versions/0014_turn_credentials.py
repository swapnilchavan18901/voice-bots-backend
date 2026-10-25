"""Persist encrypted, rotating TURN credentials across backend restarts."""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "turn_credential_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("encrypted_state", sa.Text(), nullable=False),
    )


def downgrade():
    op.drop_table("turn_credential_state")
