from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class IdentityMixin:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(IdentityMixin, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(100))


class Organization(IdentityMixin, Base):
    __tablename__ = "organizations"
    name: Mapped[str] = mapped_column(String(100))
    # Exactly one personal organization per user; additional organizations have NULL here.
    personal_for_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), unique=True)


class Membership(IdentityMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id"),
        CheckConstraint("role IN ('owner', 'admin', 'member')"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="member")


class Invitation(IdentityMixin, Base):
    __tablename__ = "invitations"
    __table_args__ = (CheckConstraint("role IN ('admin', 'member')"),)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(20))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    invited_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BotTemplate(IdentityMixin, Base):
    __tablename__ = "bot_templates"
    service_type: Mapped[str] = mapped_column(
        String(30), default="conversation", server_default="conversation"
    )
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text)
    system_prompt: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Bot(IdentityMixin, Base):
    __tablename__ = "bots"
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    template_id: Mapped[UUID] = mapped_column(ForeignKey("bot_templates.id"), index=True)
    company_context: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(String(20), default="hi-IN")


class ApiKey(IdentityMixin, Base):
    __tablename__ = "api_keys"
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VoiceGrant(IdentityMixin, Base):
    __tablename__ = "voice_grants"
    api_key_id: Mapped[UUID] = mapped_column(
        ForeignKey("api_keys.id", ondelete="CASCADE"), index=True
    )
    bot_id: Mapped[UUID] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    offer_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ClinicDoctor(Base):
    __tablename__ = "clinic_doctors"
    __table_args__ = (UniqueConstraint("bot_id", "name", name="uq_clinic_doctor_bot_name"),)
    bot_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("bots.id", ondelete="CASCADE"), index=True
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    specialty: Mapped[str] = mapped_column(String(80))


class ClinicAppointment(IdentityMixin, Base):
    __tablename__ = "clinic_appointments"
    bot_id: Mapped[UUID] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"), index=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("clinic_doctors.id"))
    patient_name: Mapped[str] = mapped_column(String(120))
    patient_phone: Mapped[str] = mapped_column(String(20))
    start_time: Mapped[datetime] = mapped_column(DateTime)  # fixed Asia/Kolkata local time
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="booked")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        CheckConstraint("status IN ('booked', 'cancelled')"),
        Index(
            "uq_clinic_active_slot",
            "bot_id",
            "doctor_id",
            "start_time",
            unique=True,
            sqlite_where=text("status = 'booked'"),
            postgresql_where=text("status = 'booked'"),
        ),
    )


class MeetingGoogleConnection(IdentityMixin, Base):
    """One operator-managed identity for the whole deployment, never tenant credentials."""

    __tablename__ = "meeting_google_connection"
    __table_args__ = (
        CheckConstraint("singleton_key = 1", name="ck_meeting_google_singleton"),
        CheckConstraint(
            "status IN ('connected', 'reauth_required', 'disabled')",
            name="ck_meeting_google_status",
        ),
    )
    singleton_key: Mapped[int] = mapped_column(unique=True, default=1, server_default="1")
    google_subject: Mapped[str] = mapped_column(String(255), unique=True)
    email: Mapped[str] = mapped_column(String(320))
    # Only encrypted envelopes may be written here. The encryption key lives outside the DB.
    refresh_token_ciphertext: Mapped[str] = mapped_column(Text)
    access_token_ciphertext: Mapped[str | None] = mapped_column(Text)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    granted_scopes: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="connected")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MeetingCalendar(IdentityMixin, Base):
    __tablename__ = "meeting_calendars"
    __table_args__ = (
        UniqueConstraint("connection_id", "google_calendar_id", name="uq_meeting_calendar"),
    )
    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("meeting_google_connection.id", ondelete="CASCADE")
    )
    google_calendar_id: Mapped[str] = mapped_column(String(320))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Meeting(IdentityMixin, Base):
    __tablename__ = "meetings"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_meeting_org_id"),
        UniqueConstraint("organization_id", "occurrence_key", name="uq_meeting_occurrence"),
        CheckConstraint("ends_at > starts_at", name="ck_meeting_time_range"),
        CheckConstraint(
            "status IN ('scheduled', 'cancelled', 'in_progress', 'completed', 'failed')",
            name="ck_meeting_status",
        ),
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    # Stable across reschedules; derived from event UID + original recurrence start.
    occurrence_key: Mapped[str] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(String(500))
    meet_url: Mapped[str] = mapped_column(Text)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(100), default="UTC")
    agenda: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="scheduled")
    auto_join: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MeetingCalendarEvent(IdentityMixin, Base):
    """Operator-only discovery inbox. No bot joins until an event is assigned a meeting."""

    __tablename__ = "meeting_calendar_events"
    __table_args__ = (
        UniqueConstraint("calendar_id", "provider_event_id", name="uq_meeting_calendar_event"),
    )
    calendar_id: Mapped[UUID] = mapped_column(
        ForeignKey("meeting_calendars.id", ondelete="CASCADE")
    )
    provider_event_id: Mapped[str] = mapped_column(String(1024))
    meeting_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("meetings.id", ondelete="SET NULL"), index=True
    )
    ical_uid: Mapped[str | None] = mapped_column(Text)
    recurring_event_id: Mapped[str | None] = mapped_column(String(1024))
    original_starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    title: Mapped[str | None] = mapped_column(Text)
    meet_url: Mapped[str | None] = mapped_column(Text)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Cancelled event tombstones may contain only the provider ID.
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)


class MeetingCapture(IdentityMixin, Base):
    """Durable Attendee voice session, scoped by the owning meeting."""

    __tablename__ = "meeting_captures"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "meeting_id"],
            ["meetings.organization_id", "meetings.id"],
            name="fk_meeting_capture_owner",
            ondelete="CASCADE",
        ),
        UniqueConstraint("meeting_id", name="uq_meeting_capture_meeting"),
    )
    organization_id: Mapped[UUID] = mapped_column(index=True)
    meeting_id: Mapped[UUID] = mapped_column(index=True)
    provider_bot_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    speaker_names: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="starting")
    detail: Mapped[str] = mapped_column(Text, default="Connecting the meeting voice agent.")
    provider_status: Mapped[str | None] = mapped_column(String(100))
    transcript: Mapped[list] = mapped_column(JSON, default=list)
    memory_status: Mapped[str] = mapped_column(String(20), default="pending")
    memory_error: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MeetingMemory(Base):
    """Exactly one rolling context string per workspace; dates are UTC."""

    __tablename__ = "meeting_memories"
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    context: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MeetingSeries(IdentityMixin, Base):
    __tablename__ = "meeting_series"
    __table_args__ = (
        UniqueConstraint("calendar_id", "provider_series_id", name="uq_meeting_series"),
    )
    calendar_id: Mapped[UUID] = mapped_column(
        ForeignKey("meeting_calendars.id", ondelete="CASCADE")
    )
    provider_series_id: Mapped[str] = mapped_column(String(1024))
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    agenda: Mapped[str] = mapped_column(Text, default="")
    auto_join: Mapped[bool] = mapped_column(Boolean, default=True)
