"""Shared occurrence assignment for the inbox and recurring calendar sync."""

import hashlib

from fastapi import HTTPException
from sqlalchemy import func, select

from app.db.models import Meeting, MeetingSeries, Membership, Organization, User, utcnow


def aware(value):
    return value.replace(tzinfo=utcnow().tzinfo) if value.tzinfo is None else value


async def assign_event(db, event, org_id, agenda="", auto_join=True):
    if event.meeting_id:
        meeting = await db.get(Meeting, event.meeting_id)
        if meeting.organization_id != org_id:
            raise HTTPException(409, "This occurrence belongs to another workspace")
        return meeting
    if (
        event.cancelled
        or not event.meet_url
        or not event.starts_at
        or not event.ends_at
        or aware(event.ends_at) <= utcnow()
        or event.ends_at <= event.starts_at
    ):
        raise HTTPException(409, "Only upcoming timed Google Meet events can be assigned")
    identity = event.ical_uid or f"{event.calendar_id}:{event.provider_event_id}"
    if event.recurring_event_id:
        if not event.original_starts_at:
            raise HTTPException(409, "Recurring event lacks its original start time")
        identity += ":" + aware(event.original_starts_at).isoformat()
    key = hashlib.sha256(identity.encode()).hexdigest()
    meeting = await db.scalar(select(Meeting).where(Meeting.occurrence_key == key))
    if meeting and meeting.organization_id != org_id:
        raise HTTPException(409, "This occurrence belongs to another workspace")
    if meeting is None:
        meeting = Meeting(
            organization_id=org_id,
            occurrence_key=key,
            title=(event.title or "Meeting")[:500],
            meet_url=event.meet_url,
            starts_at=event.starts_at,
            ends_at=event.ends_at,
            timezone="UTC",
            agenda=agenda,
            auto_join=auto_join,
        )
        db.add(meeting)
        await db.flush()
    event.meeting_id = meeting.id
    return meeting


async def organizer_organizations(db, email):
    if not email:
        return []
    return (
        await db.scalars(
            select(Organization)
            .join(Membership)
            .join(User, User.id == Membership.user_id)
            .where(func.lower(User.calendar_email) == email.strip().lower())
            .distinct()
            .order_by(Organization.name, Organization.id)
        )
    ).all()


async def auto_assign_event(db, event):
    """Only the organizer's memberships can receive their event or series."""
    if (
        event.meeting_id
        or event.cancelled
        or not event.meet_url
        or not event.starts_at
        or not event.ends_at
        or aware(event.ends_at) <= utcnow()
        or event.ends_at <= event.starts_at
        or (event.recurring_event_id and not event.original_starts_at)
    ):
        return
    organizations = await organizer_organizations(db, event.organizer_email)
    if not organizations:
        return
    if event.recurring_event_id:
        series = await db.scalar(
            select(MeetingSeries).where(
                MeetingSeries.calendar_id == event.calendar_id,
                MeetingSeries.provider_series_id == event.recurring_event_id,
                MeetingSeries.organizer_email == event.organizer_email,
            )
        )
        if series and any(org.id == series.organization_id for org in organizations):
            await assign_event(db, event, series.organization_id, series.agenda, series.auto_join)
            return
    if len(organizations) == 1:
        await assign_event(db, event, organizations[0].id)
