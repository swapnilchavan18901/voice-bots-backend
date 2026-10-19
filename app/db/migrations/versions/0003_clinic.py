"""Static clinic service and bot-scoped appointment storage."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "bot_templates",
        sa.Column("service_type", sa.String(30), nullable=False, server_default="conversation"),
    )
    op.execute("UPDATE bot_templates SET service_type = 'clinic' WHERE slug = 'clinic-booking'")
    op.execute(
        "UPDATE bot_templates SET system_prompt = replace(system_prompt, "
        "'No scheduling tools are currently connected.', "
        "'Use the connected clinic tools for scheduling.') WHERE service_type = 'clinic'"
    )
    op.create_table(
        "clinic_doctors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("specialty", sa.String(80), nullable=False),
    )
    doctors = sa.table(
        "clinic_doctors",
        sa.column("id", sa.Integer()),
        sa.column("name", sa.String()),
        sa.column("specialty", sa.String()),
    )
    op.bulk_insert(
        doctors,
        [
            {"id": 1, "name": "Dr. Anjali Mehta", "specialty": "General Physician"},
            {"id": 2, "name": "Dr. Rohan Desai", "specialty": "Pediatrician"},
            {"id": 3, "name": "Dr. Priya Kulkarni", "specialty": "Gynecologist"},
        ],
    )
    op.create_table(
        "clinic_appointments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("doctor_id", sa.Integer(), sa.ForeignKey("clinic_doctors.id"), nullable=False),
        sa.Column("patient_name", sa.String(120), nullable=False),
        sa.Column("patient_phone", sa.String(20), nullable=False),
        sa.Column("start_time", sa.DateTime(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("status", sa.String(20), nullable=False),
        sa.CheckConstraint("status IN ('booked', 'cancelled')"),
    )
    op.create_index("ix_clinic_appointments_bot_id", "clinic_appointments", ["bot_id"])
    op.create_index(
        "uq_clinic_active_slot",
        "clinic_appointments",
        ["bot_id", "doctor_id", "start_time"],
        unique=True,
        sqlite_where=sa.text("status = 'booked'"),
        postgresql_where=sa.text("status = 'booked'"),
    )


def downgrade():
    op.drop_table("clinic_appointments")
    op.drop_table("clinic_doctors")
    op.drop_column("bot_templates", "service_type")
