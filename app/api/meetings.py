"""Calendar assignment, Attendee voice control, and workspace meeting memory."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from sqlalchemy import select

from app.api.dependencies import DB, CurrentUser
from app.business.meeting_calendar import assign_event
from app.business.organizations import require_membership
from app.db.models import (
    Meeting,
    MeetingCalendar,
    MeetingCalendarEvent,
    MeetingGoogleConnection,
    MeetingSeries,
    Organization,
    utcnow,
)
from app.env import get_settings
from app.integrations.google_meet_calendar import lock
from app.schemas.api import OrgOut
from app.schemas.meetings import AssignMeeting, EventOut, MeetingOut, MeetingSchedule

router = APIRouter(tags=["meetings"])


def is_operator(user):
    return user.id in get_settings().meeting_operator_user_ids


async def require_operator(user: CurrentUser):
    if not is_operator(user):
        raise HTTPException(403, "Calendar inbox is restricted to the platform operator")
    return user


Operator = Annotated[object, Depends(require_operator)]


@router.get("/meeting-capabilities")
async def capabilities(user: CurrentUser, request: Request):
    return {
        "operator": is_operator(user),
        "voice_name": get_settings().meeting_voice_name,
        "manual_join_ready": request.app.state.meeting_voice.ready,
    }


@router.get("/operator/meeting-calendar")
async def connection_status(db: DB, operator: Operator):
    connection = await db.scalar(select(MeetingGoogleConnection))
    calendars = (await db.scalars(select(MeetingCalendar))).all()
    return {
        "status": connection.status if connection else "not_connected",
        "email": connection.email if connection else None,
        "calendars": [
            {"id": c.id, "last_synced_at": c.last_synced_at, "enabled": c.enabled}
            for c in calendars
        ],
    }


@router.get("/operator/meeting-organizations", response_model=list[OrgOut])
async def assignment_organizations(db: DB, operator: Operator):
    return (await db.scalars(select(Organization).order_by(Organization.name))).all()


@router.get("/operator/meeting-events", response_model=list[EventOut])
async def inbox(
    db: DB, operator: Operator, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    return (
        await db.scalars(
            select(MeetingCalendarEvent)
            .where(
                MeetingCalendarEvent.meeting_id.is_(None),
                MeetingCalendarEvent.cancelled.is_(False),
                MeetingCalendarEvent.ends_at > utcnow(),
            )
            .order_by(MeetingCalendarEvent.starts_at, MeetingCalendarEvent.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()


@router.post("/operator/meeting-events/{event_id}/assign", response_model=MeetingOut)
async def assign(event_id: UUID, data: AssignMeeting, db: DB, operator: Operator):
    # Same lock as sync; prevent assignment from racing a cancellation or another assignment.
    await lock(db)
    event = await db.scalar(
        select(MeetingCalendarEvent).where(MeetingCalendarEvent.id == event_id).with_for_update()
    )
    if not event:
        raise HTTPException(404, "Calendar event not found")
    if not await db.get(Organization, data.organization_id):
        raise HTTPException(404, "Workspace not found")
    meeting = await assign_event(db, event, data.organization_id, data.agenda, data.auto_join)
    if data.assign_series and event.recurring_event_id:
        series = await db.scalar(
            select(MeetingSeries).where(
                MeetingSeries.calendar_id == event.calendar_id,
                MeetingSeries.provider_series_id == event.recurring_event_id,
            )
        )
        if series and series.organization_id != data.organization_id:
            raise HTTPException(409, "This series belongs to another workspace")
        if not series:
            db.add(
                MeetingSeries(
                    calendar_id=event.calendar_id,
                    provider_series_id=event.recurring_event_id,
                    organization_id=data.organization_id,
                    agenda=data.agenda,
                    auto_join=data.auto_join,
                )
            )
        occurrences = (
            await db.scalars(
                select(MeetingCalendarEvent).where(
                    MeetingCalendarEvent.calendar_id == event.calendar_id,
                    MeetingCalendarEvent.recurring_event_id == event.recurring_event_id,
                    MeetingCalendarEvent.cancelled.is_(False),
                    MeetingCalendarEvent.ends_at > utcnow(),
                )
            )
        ).all()
        for occurrence in occurrences:
            if occurrence.meet_url and occurrence.starts_at and occurrence.original_starts_at:
                await assign_event(
                    db, occurrence, data.organization_id, data.agenda, data.auto_join
                )
    await db.commit()
    return meeting


@router.get("/organizations/{org_id}/meetings", response_model=list[MeetingOut])
async def meetings(
    org_id: UUID,
    db: DB,
    user: CurrentUser,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    await require_membership(db, org_id, user.id)
    return (
        await db.scalars(
            select(Meeting)
            .where(Meeting.organization_id == org_id)
            .order_by(Meeting.starts_at.desc(), Meeting.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()


async def owned_meeting(db, org_id, meeting_id, user_id):
    await require_membership(db, org_id, user_id)
    meeting = await db.scalar(
        select(Meeting).where(Meeting.id == meeting_id, Meeting.organization_id == org_id)
    )
    if not meeting:
        raise HTTPException(404, "Meeting not found")
    return meeting


@router.get("/organizations/{org_id}/meetings/{meeting_id}/connection")
async def join_status(org_id: UUID, meeting_id: UUID, db: DB, user: CurrentUser, request: Request):
    meeting = await owned_meeting(db, org_id, meeting_id, user.id)
    service = request.app.state.meeting_voice
    capture = await service.capture(db, meeting.id)
    return {**service.output(capture, meeting.id), "auto_join": meeting.auto_join}


@router.post("/organizations/{org_id}/meetings/{meeting_id}/join", status_code=202)
async def join_now(org_id: UUID, meeting_id: UUID, db: DB, operator: Operator, request: Request):
    meeting = await owned_meeting(db, org_id, meeting_id, operator.id)
    if meeting.status == "cancelled":
        raise HTTPException(409, "Cancelled meetings cannot be joined")
    return await request.app.state.meeting_voice.start(db, meeting)


@router.post("/organizations/{org_id}/meetings/{meeting_id}/leave")
async def leave_now(org_id: UUID, meeting_id: UUID, db: DB, operator: Operator, request: Request):
    meeting = await owned_meeting(db, org_id, meeting_id, operator.id)
    return await request.app.state.meeting_voice.stop(db, meeting)


@router.websocket("/meeting-audio/{capture_id}")
async def meeting_audio(websocket: WebSocket, capture_id: UUID, token: str = ""):
    await websocket.app.state.meeting_voice.audio(websocket, capture_id, token)


@router.get("/organizations/{org_id}/meeting-memory")
async def meeting_memory(org_id: UUID, db: DB, user: CurrentUser, request: Request):
    await require_membership(db, org_id, user.id)
    return {"context": await request.app.state.meeting_voice.memory.read(org_id)}


@router.put("/organizations/{org_id}/meetings/{meeting_id}/schedule", response_model=MeetingOut)
async def schedule_meeting(
    org_id: UUID,
    meeting_id: UUID,
    data: MeetingSchedule,
    db: DB,
    operator: Operator,
):
    meeting = await owned_meeting(db, org_id, meeting_id, operator.id)
    await db.refresh(meeting, with_for_update=True)
    if meeting.status != "scheduled" or meeting.ends_at <= utcnow():
        raise HTTPException(409, "Only upcoming scheduled meetings can change auto-join")
    meeting.auto_join = data.auto_join
    await db.commit()
    return meeting
