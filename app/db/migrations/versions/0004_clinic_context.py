"""Scope doctor records to bots; their source is now each bot's saved context."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("clinic_doctors") as batch:
        batch.add_column(sa.Column("bot_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_clinic_doctors_bot", "bots", ["bot_id"], ["id"], ondelete="CASCADE"
        )
        batch.create_index("ix_clinic_doctors_bot_id", ["bot_id"])
        batch.create_unique_constraint("uq_clinic_doctor_bot_name", ["bot_id", "name"])
    # Legacy shared doctors remain only for historical foreign keys, never as defaults.


def downgrade():
    with op.batch_alter_table("clinic_doctors") as batch:
        batch.drop_constraint("uq_clinic_doctor_bot_name", type_="unique")
        batch.drop_constraint("fk_clinic_doctors_bot", type_="foreignkey")
        batch.drop_index("ix_clinic_doctors_bot_id")
        batch.drop_column("bot_id")
