"""Organizer-only discovery and membership-scoped assignment, including direct API access."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from test_platform import account

from app.db.models import (
    Meeting,
    MeetingCalendar,
    MeetingCalendarEvent,
    MeetingGoogleConnection,
    Membership,
    User,
    utcnow,
)
from app.integrations.google_meet_calendar import apply_event


async def calendar(sessions):
    async with sessions() as db:
        # Model the trusted operator linking these known test accounts.
        for user in (await db.scalars(select(User))).all():
            user.calendar_email = user.email.lower()
        connection = MeetingGoogleConnection(
            google_subject="shared-bot",
            email="bot@example.com",
            refresh_token_ciphertext="test-only",
            granted_scopes="calendar.events.readonly",
        )
        db.add(connection)
        await db.flush()
        result = MeetingCalendar(connection_id=connection.id, google_calendar_id="primary")
        db.add(result)
        await db.commit()
        return result.id


def event_data(email, provider_id="event", series=None):
    start = utcnow() + timedelta(days=1)
    data = {
        "id": provider_id,
        "iCalUID": provider_id + "@google",
        "summary": "Private planning",
        "organizer": {"email": email},
        "hangoutLink": "https://meet.google.com/abc-defg-hij",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(hours=1)).isoformat()},
    }
    if series:
        data.update(recurringEventId=series, originalStartTime={"dateTime": start.isoformat()})
    return data


async def add_event(sessions, calendar_id, data):
    async with sessions() as db:
        event = await apply_event(db, calendar_id, data)
        await db.commit()
        return event.id, event.meeting_id


async def test_single_organization_auto_assigns_and_sync_is_idempotent(client):
    c, sessions = client
    headers, org = await account(c, "owner@example.com")
    calendar_id = await calendar(sessions)
    data = event_data("OWNER@EXAMPLE.COM")
    event_id, meeting_id = await add_event(sessions, calendar_id, data)
    assert meeting_id is not None
    assert await add_event(sessions, calendar_id, data) == (event_id, meeting_id)
    meetings = (await c.get(f"/api/v1/organizations/{org}/meetings", headers=headers)).json()
    assert [meeting["id"] for meeting in meetings] == [str(meeting_id)]
    inbox = await c.get("/api/v1/operator/meeting-events", headers=headers)
    assert inbox.status_code == 200 and inbox.json() == []
    async with sessions() as db:
        meeting = await db.get(Meeting, meeting_id)
        assert str(meeting.organization_id) == org


async def test_multi_org_choices_private_even_from_workspace_owner_and_operator(
    client, monkeypatch
):
    from app.api import meetings as api

    c, sessions = client
    person, personal_org = await account(c, "person@example.com")
    owner, shared_org = await account(c, "owner@example.com")
    operator, unrelated_org = await account(c, "operator@example.com")
    async with sessions() as db:
        person_user = await db.scalar(select(User).where(User.email == "person@example.com"))
        operator_user = await db.scalar(select(User).where(User.email == "operator@example.com"))
        db.add(Membership(user_id=person_user.id, organization_id=UUID(shared_org), role="member"))
        await db.commit()
        monkeypatch.setattr(api, "is_operator", lambda user: user.id == operator_user.id)
    calendar_id = await calendar(sessions)
    event_id, meeting_id = await add_event(sessions, calendar_id, event_data("person@example.com"))
    assert meeting_id is None
    base = "/api/v1/operator"
    choices = await c.get(base + "/meeting-organizations", headers=person)
    assert {o["id"] for o in choices.json()} == {personal_org, shared_org}
    # Organization owners and global operators see only their own memberships and inbox.
    for headers, own_org in [(owner, shared_org), (operator, unrelated_org)]:
        assert (await c.get(base + "/meeting-events", headers=headers)).json() == []
        assert {
            o["id"] for o in (await c.get(base + "/meeting-organizations", headers=headers)).json()
        } == {own_org}
        assert (
            await c.post(
                base + f"/meeting-events/{event_id}/assign",
                headers=headers,
                json={"organization_id": own_org},
            )
        ).status_code == 404
    inbox = (await c.get(base + "/meeting-events", headers=person)).json()
    assert [e["id"] for e in inbox] == [str(event_id)]
    assert (
        await c.post(
            base + f"/meeting-events/{event_id}/assign",
            headers=person,
            json={"organization_id": unrelated_org},
        )
    ).status_code == 404
    response = await c.post(
        base + f"/meeting-events/{event_id}/assign",
        headers=person,
        json={"organization_id": shared_org, "auto_join": False},
    )
    assert response.status_code == 200, response.text
    assert response.json()["organization_id"] == shared_org
    # Only the assigned meeting becomes workspace-visible; choices stay personal.
    assert (
        len((await c.get(f"/api/v1/organizations/{shared_org}/meetings", headers=owner)).json())
        == 1
    )
    assert (await c.get(base + "/meeting-events", headers=person)).json() == []


async def test_missing_or_unknown_organizer_does_not_guess_an_owner(client):
    c, sessions = client
    headers, org = await account(c, "owner@example.com")
    calendar_id = await calendar(sessions)
    unknown = event_data("unregistered@example.com", "unknown")
    absent = event_data("", "missing")
    absent.pop("organizer")
    for data in (unknown, absent):
        _, meeting_id = await add_event(sessions, calendar_id, data)
        assert meeting_id is None
    assert (await c.get("/api/v1/operator/meeting-events", headers=headers)).json() == []
    assert (await c.get(f"/api/v1/organizations/{org}/meetings", headers=headers)).json() == []


async def test_series_assignment_only_applies_to_matching_organizer(client):
    c, sessions = client
    headers, personal_org = await account(c, "person@example.com")
    other_headers, shared_org = await account(c, "other@example.com")
    async with sessions() as db:
        users = (await db.scalars(select(User))).all()
        person = next(user for user in users if user.email == "person@example.com")
        other = next(user for user in users if user.email == "other@example.com")
        db.add_all(
            [
                Membership(user_id=person.id, organization_id=UUID(shared_org), role="member"),
                Membership(user_id=other.id, organization_id=UUID(personal_org), role="member"),
            ]
        )
        await db.commit()
    calendar_id = await calendar(sessions)
    event_id, _ = await add_event(
        sessions, calendar_id, event_data("person@example.com", "first", "series")
    )
    other_id, _ = await add_event(
        sessions, calendar_id, event_data("other@example.com", "other", "series")
    )
    result = await c.post(
        f"/api/v1/operator/meeting-events/{event_id}/assign",
        headers=headers,
        json={"organization_id": shared_org, "assign_series": True, "auto_join": False},
    )
    assert result.status_code == 200, result.text
    async with sessions() as db:
        assert (await db.get(MeetingCalendarEvent, other_id)).meeting_id is None
    _, future_id = await add_event(
        sessions, calendar_id, event_data("person@example.com", "future", "series")
    )
    async with sessions() as db:
        future = await db.get(Meeting, future_id)
        assert str(future.organization_id) == shared_org and future.auto_join is False
    _, other_future = await add_event(
        sessions, calendar_id, event_data("other@example.com", "other-future", "series")
    )
    assert other_future is None
    assert len((await c.get("/api/v1/operator/meeting-events", headers=other_headers)).json()) == 2


async def test_signup_email_alone_cannot_claim_calendar_events(client):
    c, sessions = client
    headers, org = await account(c, "claimed@example.com")
    calendar_id = await calendar(sessions)
    async with sessions() as db:
        user = await db.scalar(select(User).where(User.email == "claimed@example.com"))
        user.calendar_email = None  # No trusted ownership link exists for this signup.
        await db.commit()
    event_id, meeting_id = await add_event(sessions, calendar_id, event_data("claimed@example.com"))
    assert meeting_id is None
    assert (await c.get("/api/v1/operator/meeting-events", headers=headers)).json() == []
    status = (await c.get("/api/v1/operator/meeting-calendar", headers=headers)).json()
    assert status["identity_ready"] is False
    result = await c.post(
        f"/api/v1/operator/meeting-events/{event_id}/assign",
        headers=headers,
        json={"organization_id": org},
    )
    assert result.status_code == 404
    # Even organizer-less legacy rows cannot become visible through a NULL identity match.
    _, _ = await add_event(sessions, calendar_id, event_data("", "legacy"))
    assert (await c.get("/api/v1/operator/meeting-events", headers=headers)).json() == []


async def test_assignment_rechecks_membership_after_choices_were_loaded(client):
    from sqlalchemy import delete

    c, sessions = client
    headers, _ = await account(c, "person@example.com")
    _, shared_org = await account(c, "owner@example.com")
    async with sessions() as db:
        user = await db.scalar(select(User).where(User.email == "person@example.com"))
        user_id = user.id
        db.add(Membership(user_id=user_id, organization_id=UUID(shared_org), role="member"))
        await db.commit()
    calendar_id = await calendar(sessions)
    event_id, _ = await add_event(sessions, calendar_id, event_data("person@example.com"))
    async with sessions() as db:
        await db.execute(
            delete(Membership).where(
                Membership.user_id == user_id, Membership.organization_id == UUID(shared_org)
            )
        )
        await db.commit()
    assert (
        await c.post(
            f"/api/v1/operator/meeting-events/{event_id}/assign",
            headers=headers,
            json={"organization_id": shared_org},
        )
    ).status_code == 404


async def test_trusted_link_command_checks_identity_and_can_revoke(client, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import pytest

    from app.core import meeting_google
    from app.integrations.google_meet_calendar import CalendarError

    c, sessions = client
    await account(c, "person@example.com")
    async with sessions() as db:
        user = await db.scalar(select(User).where(User.email == "person@example.com"))
        user_id = user.id
    monkeypatch.setattr(meeting_google, "SessionFactory", sessions)
    monkeypatch.setattr(meeting_google, "engine", SimpleNamespace(dispose=AsyncMock()))
    with pytest.raises(CalendarError, match="must match"):
        await meeting_google.link_organizer(user_id, "someone-else@example.com")
    await meeting_google.link_organizer(user_id, "PERSON@EXAMPLE.COM")
    async with sessions() as db:
        assert (await db.get(User, user_id)).calendar_email == "person@example.com"
    await meeting_google.link_organizer(user_id)
    async with sessions() as db:
        assert (await db.get(User, user_id)).calendar_email is None
