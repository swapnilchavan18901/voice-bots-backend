"""One reusable calendar definition; captures retain actual occurrence history.

Run with the application stopped and a database backup. Historical capture IDs,
provider IDs, transcripts and memory are preserved. Conflicting workspace ownership
fails before schema changes rather than merging conversations across tenants.
"""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def aware(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def upgrade():
    connection = op.get_bind()
    metadata = sa.MetaData()
    old_meetings = sa.Table("meetings", metadata, autoload_with=connection)
    events = sa.Table("meeting_calendar_events", metadata, autoload_with=connection)
    series = sa.Table("meeting_series", metadata, autoload_with=connection)
    captures = sa.Table("meeting_captures", metadata, autoload_with=connection)
    meetings = {r["id"]: dict(r) for r in connection.execute(old_meetings.select()).mappings()}
    capture_rows = [dict(r) for r in connection.execute(captures.select()).mappings()]
    groups = defaultdict(list)
    for row in connection.execute(events.select()).mappings():
        groups[(row["calendar_id"], row["recurring_event_id"] or row["provider_event_id"])].append(
            dict(row)
        )
    preferences = {
        (r["calendar_id"], r["provider_series_id"]): dict(r)
        for r in connection.execute(series.select()).mappings()
    }
    for key in preferences:
        groups.setdefault(key, [])
    membership = {}
    for key, rows in groups.items():
        ids = {r["meeting_id"] for r in rows if r["meeting_id"] in meetings}
        owners = {meetings[mid]["organization_id"] for mid in ids}
        if key in preferences:
            owners.add(preferences[key]["organization_id"])
        if len(owners) > 1:
            raise RuntimeError(
                "Cannot consolidate a recurring series assigned to multiple organizations. Resolve its ownership before migration."
            )
        for mid in ids:
            if mid in membership and membership[mid] != key:
                raise RuntimeError(
                    "A meeting is linked to multiple calendars. Resolve duplicate calendar ownership before migration."
                )
            membership[mid] = key
    # Standalone legacy rows without calendar links still retain their session history.
    for mid in meetings.keys() - membership.keys():
        key = (None, "legacy:" + str(mid))
        groups[key] = []
        membership[mid] = key

    with op.batch_alter_table("meetings") as batch:
        batch.alter_column("organization_id", existing_type=sa.Uuid(), nullable=True)
        batch.add_column(
            sa.Column(
                "calendar_id",
                sa.Uuid(),
                sa.ForeignKey("meeting_calendars.id", name="fk_meeting_calendar"),
            )
        )
        batch.add_column(sa.Column("provider_event_id", sa.String(1024)))
        batch.add_column(sa.Column("organizer_email", sa.String(320)))
        batch.add_column(sa.Column("is_recurring", sa.Boolean()))
        batch.add_column(sa.Column("recurrence", sa.JSON()))
        batch.add_column(sa.Column("next_occurrence_id", sa.String(1024)))
        batch.add_column(sa.Column("next_starts_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("next_ends_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("next_meet_url", sa.Text()))
        batch.add_column(sa.Column("next_title", sa.String(500)))
    with op.batch_alter_table("meeting_captures") as batch:
        batch.drop_constraint("uq_meeting_capture_meeting", type_="unique")
        batch.add_column(sa.Column("provider_occurrence_id", sa.String(1024)))
        batch.add_column(sa.Column("scheduled_starts_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("scheduled_ends_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("title", sa.String(500)))
        batch.add_column(sa.Column("meet_url", sa.Text()))
        batch.add_column(sa.Column("agenda", sa.Text()))

    current = sa.Table("meetings", sa.MetaData(), autoload_with=connection)
    current_captures = sa.Table("meeting_captures", sa.MetaData(), autoload_with=connection)
    now = datetime.now(UTC)
    replacements = {}
    capture_updates = []
    for key, rows in groups.items():
        linked = sorted(
            [m for mid, m in meetings.items() if membership.get(mid) == key],
            key=lambda m: (aware(m["created_at"]), str(m["id"])),
        )
        preference = preferences.get(key)
        canonical = dict(linked[0]) if linked else None
        canonical_id = canonical["id"] if canonical else uuid4()
        ids = {m["id"] for m in linked}
        attempted = set()
        for capture in capture_rows:
            if capture["meeting_id"] not in ids:
                continue
            original = meetings[capture["meeting_id"]]
            matching = sorted(
                [e for e in rows if e["meeting_id"] == original["id"]], key=lambda e: str(e["id"])
            )
            occurrence_id = (
                matching[0]["provider_event_id"] if matching else "legacy:" + str(original["id"])
            )
            if occurrence_id in attempted:
                raise RuntimeError(
                    "Duplicate historical captures for one occurrence require manual reconciliation before migration."
                )
            attempted.add(occurrence_id)
            capture_updates.append(
                (
                    capture["id"],
                    {
                        "meeting_id": canonical_id,
                        "provider_occurrence_id": occurrence_id,
                        "scheduled_starts_at": original["starts_at"],
                        "scheduled_ends_at": original["ends_at"],
                        "title": original["title"],
                        "meet_url": original["meet_url"],
                        "agenda": original["agenda"],
                    },
                )
            )
        upcoming = sorted(
            [
                e
                for e in rows
                if not e["cancelled"]
                and e["meet_url"]
                and e["starts_at"]
                and e["ends_at"]
                and aware(e["ends_at"]) > now
                and aware(e["ends_at"]) > aware(e["starts_at"])
                and e["provider_event_id"] not in attempted
            ],
            key=lambda e: (aware(e["starts_at"]), e["provider_event_id"]),
        )
        selected = upcoming[0] if upcoming else None
        if (
            not rows
            and canonical
            and not attempted
            and aware(canonical["ends_at"]) > now
            and canonical["status"] != "cancelled"
        ):
            selected = {**canonical, "provider_event_id": key[1]}
        recurring = bool(preference or any(e["recurring_event_id"] for e in rows))
        source = selected or (rows[0] if rows else {})
        values = {
            "calendar_id": key[0],
            "provider_event_id": key[1],
            "is_recurring": recurring,
            "recurrence": [],
            "organizer_email": preference["organizer_email"]
            if preference
            else source.get("organizer_email"),
            "organization_id": preference["organization_id"]
            if preference
            else (canonical["organization_id"] if canonical else None),
            "agenda": preference["agenda"]
            if preference
            else (canonical["agenda"] if canonical else ""),
            "auto_join": preference["auto_join"]
            if preference
            else (canonical["auto_join"] if canonical else False),
            "next_occurrence_id": selected["provider_event_id"] if selected else None,
            "next_starts_at": selected["starts_at"] if selected else None,
            "next_ends_at": selected["ends_at"] if selected else None,
            "next_meet_url": selected["meet_url"] if selected else None,
            "next_title": (selected["title"] or "Meeting")[:500] if selected else None,
            "status": "scheduled"
            if recurring or selected
            else ("cancelled" if source.get("cancelled") else "completed"),
        }
        if canonical:
            connection.execute(
                current.update().where(current.c.id == canonical_id).values(**values)
            )
        else:
            # Legacy required columns are removed after consolidation.
            connection.execute(
                current.insert().values(
                    id=canonical_id,
                    created_at=now,
                    updated_at=now,
                    occurrence_key=str(canonical_id),
                    title=(source.get("title") or "Meeting")[:500],
                    meet_url=source.get("meet_url") or "",
                    starts_at=now,
                    ends_at=now + timedelta(seconds=1),
                    timezone="UTC",
                    **values,
                )
            )
        for mid in ids:
            replacements[mid] = canonical_id

    for capture_id, values in capture_updates:
        connection.execute(
            current_captures.update().where(current_captures.c.id == capture_id).values(**values)
        )
    op.drop_table("meeting_calendar_events")
    op.drop_table("meeting_series")
    for old_id, canonical_id in replacements.items():
        if old_id != canonical_id:
            connection.execute(current.delete().where(current.c.id == old_id))
    with op.batch_alter_table("meeting_captures") as batch:
        for name, type_ in (
            ("provider_occurrence_id", sa.String(1024)),
            ("scheduled_starts_at", sa.DateTime(timezone=True)),
            ("scheduled_ends_at", sa.DateTime(timezone=True)),
            ("title", sa.String(500)),
            ("meet_url", sa.Text()),
            ("agenda", sa.Text()),
        ):
            batch.alter_column(name, existing_type=type_, nullable=False)
        batch.create_unique_constraint(
            "uq_meeting_capture_occurrence", ["meeting_id", "provider_occurrence_id"]
        )
    with op.batch_alter_table("meetings") as batch:
        batch.drop_constraint("ck_meeting_time_range", type_="check")
        batch.drop_constraint("ck_meeting_status", type_="check")
        batch.drop_constraint("uq_meeting_occurrence", type_="unique")
        batch.drop_index("ix_meetings_starts_at")
        for name in ("starts_at", "ends_at", "occurrence_key"):
            batch.drop_column(name)
        for name, type_ in (
            ("provider_event_id", sa.String(1024)),
            ("is_recurring", sa.Boolean()),
            ("recurrence", sa.JSON()),
        ):
            batch.alter_column(name, existing_type=type_, nullable=False)
        batch.create_unique_constraint(
            "uq_meeting_definition", ["calendar_id", "provider_event_id"]
        )
        batch.create_check_constraint(
            "ck_meeting_status", "status IN ('scheduled', 'cancelled', 'completed')"
        )
        batch.create_check_constraint(
            "ck_meeting_next_occurrence",
            "(next_occurrence_id IS NULL AND next_starts_at IS NULL AND next_ends_at IS NULL) OR "
            "(next_occurrence_id IS NOT NULL AND next_starts_at IS NOT NULL AND next_ends_at IS NOT NULL AND next_ends_at > next_starts_at)",
        )
        batch.create_index("ix_meetings_organizer_email", ["organizer_email"])
        batch.create_index("ix_meetings_next_starts_at", ["next_starts_at"])


def downgrade():
    raise RuntimeError(
        "Meeting occurrences have been consolidated. Restore the pre-migration backup to downgrade without inventing historical calendar rows."
    )
