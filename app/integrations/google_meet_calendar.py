"""Operator-only, read-only Calendar integration. No Meet browser authentication."""

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode, urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select, text

from app.business.meeting_calendar import assign_event
from app.db.models import (
    Meeting,
    MeetingCalendar,
    MeetingCalendarEvent,
    MeetingGoogleConnection,
    MeetingSeries,
    utcnow,
)

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
SCOPES = f"openid email {CALENDAR_SCOPE}"
TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{}/events"
LOCK_ID = 741029183


class CalendarError(Exception):
    """Safe operator-facing message: never include provider bodies or credentials."""


class ReauthorizationRequired(CalendarError):
    pass


def cipher(settings):
    try:
        return Fernet(settings.meeting_token_key.get_secret_value().encode())
    except (ValueError, TypeError):
        raise CalendarError("Set MEETING_TOKEN_KEY to a valid Fernet key.") from None


def validate_configuration(settings):
    if not settings.google_client_id or not settings.meeting_bot_email:
        raise CalendarError("Configure GOOGLE_CLIENT_ID and MEETING_BOT_EMAIL first.")
    cipher(settings)


def decrypt(settings, value):
    try:
        return cipher(settings).decrypt(value.encode()).decode()
    except InvalidToken:
        raise CalendarError("Cannot decrypt credentials; check MEETING_TOKEN_KEY.") from None


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def parse_instant(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise CalendarError("Google returned a timestamp without a timezone.")
    return parsed.astimezone(timezone.utc)


def oauth_request(settings, redirect_uri):
    validate_configuration(settings)
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge.decode(),
            "code_challenge_method": "S256",
            "login_hint": settings.meeting_bot_email,
        }
    )
    return url, state, verifier


async def lock(db):
    # Cross-process serialization in production, including first connection creation.
    if db.bind.dialect.name == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_ID})


async def request(client, method, url, **kwargs):
    try:
        return await client.request(method, url, **kwargs)
    except httpx.HTTPError:
        raise CalendarError("Google request failed; retry later.") from None


def body(response):
    if response.status_code >= 400:
        raise CalendarError(
            f"Google request failed (HTTP {response.status_code}); retry or reconnect."
        )
    try:
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError
        return result
    except ValueError:
        raise CalendarError("Google returned an invalid response.") from None


def token_fields(data):
    try:
        token = data["access_token"]
        seconds = int(data["expires_in"])
        if not isinstance(token, str) or not token or seconds <= 0:
            raise ValueError
        return token, utcnow() + timedelta(seconds=seconds)
    except (KeyError, TypeError, ValueError):
        raise CalendarError("Google did not return a usable access token.") from None


async def connect(db, client, settings, code, verifier, redirect_uri):
    validate_configuration(settings)
    await lock(db)
    existing = await db.scalar(select(MeetingGoogleConnection))
    response = await request(
        client,
        "POST",
        TOKEN_URL,
        data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret.get_secret_value(),
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    tokens = body(response)
    access_token, expires = token_fields(tokens)
    scopes = set(tokens.get("scope", "").split())
    if CALENDAR_SCOPE not in scopes:
        raise CalendarError("Read-only calendar permission was not granted; reconnect.")
    identity = body(
        await request(
            client,
            "GET",
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    )
    if (
        identity.get("email_verified") is not True
        or identity.get("email", "").casefold() != settings.meeting_bot_email.casefold()
        or not identity.get("sub")
    ):
        raise CalendarError("Sign in with the configured, verified MEETING_BOT_EMAIL account.")
    if existing and existing.google_subject != identity["sub"]:
        raise CalendarError(
            "A different bot account is already connected; account replacement is disabled."
        )
    refresh = tokens.get("refresh_token")
    if not refresh and not existing:
        raise CalendarError(
            "No refresh token returned. Revoke this app grant and reconnect with consent."
        )
    if existing is None:
        existing = MeetingGoogleConnection(google_subject=identity["sub"], email=identity["email"])
        db.add(existing)
    f = cipher(settings)
    if refresh:
        existing.refresh_token_ciphertext = f.encrypt(refresh.encode()).decode()
    existing.access_token_ciphertext = f.encrypt(access_token.encode()).decode()
    existing.access_token_expires_at = expires
    existing.granted_scopes = " ".join(sorted(scopes))
    existing.status = "connected"
    existing.updated_at = utcnow()
    await db.flush()
    calendar = await db.scalar(
        select(MeetingCalendar).where(
            MeetingCalendar.connection_id == existing.id,
            MeetingCalendar.google_calendar_id == "primary",
        )
    )
    if calendar is None:
        db.add(MeetingCalendar(connection_id=existing.id, google_calendar_id="primary"))
    await db.commit()


async def access_token(db, client, settings, *, force=False):
    validate_configuration(settings)
    await lock(db)
    connection = await db.scalar(
        select(MeetingGoogleConnection).execution_options(populate_existing=True)
    )
    if connection is None or connection.status != "connected":
        raise ReauthorizationRequired("Run the operator connect command first.")
    if connection.email.casefold() != settings.meeting_bot_email.casefold():
        raise CalendarError("Configured email does not match the connected bot account.")
    if (
        not force
        and connection.access_token_ciphertext
        and connection.access_token_expires_at
        and aware(connection.access_token_expires_at) > utcnow() + timedelta(minutes=2)
    ):
        token = decrypt(settings, connection.access_token_ciphertext)
        await db.commit()
        return token
    response = await request(
        client,
        "POST",
        TOKEN_URL,
        data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret.get_secret_value(),
            "grant_type": "refresh_token",
            "refresh_token": decrypt(settings, connection.refresh_token_ciphertext),
        },
    )
    if response.status_code == 400:
        try:
            invalid = response.json().get("error") == "invalid_grant"
        except (ValueError, AttributeError):
            invalid = False
        if invalid:
            connection.status = "reauth_required"
            connection.updated_at = utcnow()
            await db.commit()
            raise ReauthorizationRequired(
                "Google consent expired or was revoked; reconnect the bot."
            )
    tokens = body(response)
    token, expires = token_fields(tokens)
    f = cipher(settings)
    connection.access_token_ciphertext = f.encrypt(token.encode()).decode()
    connection.access_token_expires_at = expires
    if tokens.get("refresh_token"):
        connection.refresh_token_ciphertext = f.encrypt(tokens["refresh_token"].encode()).decode()
    connection.updated_at = utcnow()
    # Persist rotated credentials even if a later calendar request fails.
    await db.commit()
    return token


def meet_link(event):
    candidates = [event.get("hangoutLink")]
    candidates += [
        e.get("uri")
        for e in event.get("conferenceData", {}).get("entryPoints", [])
        if e.get("entryPointType") == "video"
    ]
    for value in candidates:
        if not value:
            continue
        parsed = urlparse(value)
        if (
            parsed.scheme == "https"
            and parsed.netloc == "meet.google.com"
            and parsed.path.strip("/")
        ):
            return value
    return None


async def apply_event(db, calendar_id, data):
    event = await db.scalar(
        select(MeetingCalendarEvent).where(
            MeetingCalendarEvent.calendar_id == calendar_id,
            MeetingCalendarEvent.provider_event_id == data["id"],
        )
    )
    if event is None:
        event = MeetingCalendarEvent(calendar_id=calendar_id, provider_event_id=data["id"])
        db.add(event)
    event.cancelled = data.get("status") == "cancelled"
    event.provider_updated_at = parse_instant(data.get("updated"))
    if not event.cancelled:
        event.ical_uid = data.get("iCalUID")
        event.recurring_event_id = data.get("recurringEventId")
        event.original_starts_at = parse_instant(data.get("originalStartTime", {}).get("dateTime"))
        event.title = data.get("summary")
        event.meet_url = meet_link(data)
        event.starts_at = parse_instant(data.get("start", {}).get("dateTime"))
        event.ends_at = parse_instant(data.get("end", {}).get("dateTime"))
        # All-day/non-Meet events are stored but never eligible for dispatch.
    if event.meeting_id:
        meeting = await db.get(Meeting, event.meeting_id)
        if meeting and meeting.status == "scheduled":
            valid = (
                not event.cancelled
                and event.meet_url
                and event.starts_at
                and event.ends_at
                and event.ends_at > event.starts_at
            )
            if valid:
                meeting.starts_at, meeting.ends_at = event.starts_at, event.ends_at
                meeting.meet_url = event.meet_url
                meeting.title = (event.title or "Meeting")[:500]
            else:
                meeting.status = "cancelled"
            meeting.updated_at = utcnow()
    if (
        not event.meeting_id
        and event.recurring_event_id
        and not event.cancelled
        and event.meet_url
        and event.starts_at
        and event.ends_at
        and event.original_starts_at
        and event.ends_at > event.starts_at
        and event.ends_at > utcnow()
    ):
        series = await db.scalar(
            select(MeetingSeries).where(
                MeetingSeries.calendar_id == calendar_id,
                MeetingSeries.provider_series_id == event.recurring_event_id,
            )
        )
        if series:
            await assign_event(db, event, series.organization_id, series.agenda, series.auto_join)
    return event


class Unauthorized(CalendarError):
    pass


async def calendar_get(client, url, token, **kwargs):
    response = await request(
        client, "GET", url, headers={"Authorization": f"Bearer {token}"}, **kwargs
    )
    if response.status_code == 401:
        raise Unauthorized("Google rejected the access token.")
    return response


async def sync_calendar(db, client, calendar, token, settings, now):
    # A bounded snapshot expands recurring events, including new occurrences each day.
    # Never combine this moving window with Google's incremental syncToken.
    start, end = now - timedelta(days=1), now + timedelta(days=settings.meeting_sync_days)
    url = EVENTS_URL.format(quote(calendar.google_calendar_id, safe=""))
    params = {
        "singleEvents": "true",
        "showDeleted": "true",
        "maxResults": 2500,
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "timeZone": "UTC",
    }
    seen, pages = set(), set()
    count = 0
    while True:
        data = body(await calendar_get(client, url, token, params=params))
        for item in data.get("items", []):
            await apply_event(db, calendar.id, item)
            seen.add(item["id"])
            count += 1
        page = data.get("nextPageToken")
        if not page:
            break
        if page in pages:
            raise CalendarError("Google repeated a pagination token; sync rolled back.")
        pages.add(page)
        params["pageToken"] = page
    await db.flush()
    # A disappeared event might have moved outside the window. Look it up rather
    # than treating absence as cancellation. Only reconcile previously timed events.
    previous = (
        await db.scalars(
            select(MeetingCalendarEvent).where(
                MeetingCalendarEvent.calendar_id == calendar.id,
                MeetingCalendarEvent.cancelled.is_(False),
                MeetingCalendarEvent.ends_at > start,
                MeetingCalendarEvent.starts_at < end,
            )
        )
    ).all()
    for event in previous:
        if event.provider_event_id in seen:
            continue
        response = await calendar_get(
            client, url + "/" + quote(event.provider_event_id, safe=""), token
        )
        item = (
            {"id": event.provider_event_id, "status": "cancelled"}
            if response.status_code in (404, 410)
            else body(response)
        )
        await apply_event(db, calendar.id, item)
    calendar.last_synced_at = now
    calendar.sync_token = None
    return count


async def sync(db, client, settings):
    token = await access_token(db, client, settings)
    for attempt in range(2):
        try:
            await lock(db)
            calendars = (
                await db.scalars(
                    select(MeetingCalendar).where(
                        MeetingCalendar.enabled.is_(True),
                    )
                )
            ).all()
            count = 0
            for calendar in calendars:
                count += await sync_calendar(db, client, calendar, token, settings, utcnow())
            await db.commit()
            return {"calendars": len(calendars), "events": count}
        except Unauthorized:
            await db.rollback()
            if attempt:
                raise CalendarError(
                    "Google rejected refreshed credentials; reconnect the bot."
                ) from None
            token = await access_token(db, client, settings, force=True)
        except Exception:
            await db.rollback()
            raise
