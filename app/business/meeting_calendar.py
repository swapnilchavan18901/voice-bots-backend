"""Assign a single calendar definition to its organizer's workspace."""

from fastapi import HTTPException
from sqlalchemy import func, select

from app.db.models import Membership, Organization, User, utcnow


def aware(value):
    return value.replace(tzinfo=utcnow().tzinfo) if value.tzinfo is None else value


async def assign_event(db, meeting, org_id, agenda="", auto_join=True):
    if meeting.organization_id:
        if meeting.organization_id != org_id:
            raise HTTPException(409, "This meeting series belongs to another workspace")
        return meeting
    if (
        meeting.status == "cancelled"
        or not meeting.next_occurrence_id
        or not meeting.next_meet_url
        or not meeting.next_ends_at
        or aware(meeting.next_ends_at) <= utcnow()
    ):
        raise HTTPException(409, "Only upcoming timed Google Meet definitions can be assigned")
    meeting.organization_id = org_id
    meeting.agenda = agenda
    meeting.auto_join = auto_join
    await db.flush()
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


async def auto_assign_event(db, meeting):
    if (
        meeting.organization_id
        or meeting.status == "cancelled"
        or not meeting.next_occurrence_id
        or not meeting.next_meet_url
        or not meeting.next_ends_at
        or aware(meeting.next_ends_at) <= utcnow()
    ):
        return
    organizations = await organizer_organizations(db, meeting.organizer_email)
    if len(organizations) == 1:
        await assign_event(db, meeting, organizations[0].id)
