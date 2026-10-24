"""Authenticated clinic phone controls and scoped Exotel media endpoints."""

import asyncio
from contextlib import suppress
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response, WebSocket
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.websockets import WebSocketDisconnect

from app.api.dependencies import DB, CurrentUser
from app.business import bots
from app.business.clinic_phone import clinic_bot, network_phone
from app.business.organizations import require_membership
from app.clinic_phone_types import (
    PhoneCallOut,
    PhoneDial,
    PhoneReconcile,
    PhoneRouteConfig,
    PhoneRouteOut,
)
from app.constants import constants
from app.constants.constants import MANAGE_ROLES
from app.db.models import ClinicPhoneCall, ClinicPhoneRoute, utcnow

router = APIRouter(
    prefix="/organizations/{org_id}/bots/{bot_id}/phone", tags=["Clinic phone"]
)
provider_router = APIRouter(prefix="/clinic-phone/exotel", tags=["Clinic Exotel"])


@router.get("/capabilities")
async def capabilities(
    org_id: UUID, bot_id: UUID, db: DB, user: CurrentUser, request: Request
):
    await clinic_bot(db, org_id, bot_id, user.id)
    member = await require_membership(db, org_id, user.id)
    service = request.app.state.clinic_phone
    manager = member.role in MANAGE_ROLES
    try:
        service.require_ready()
        inbound_ready = True
    except HTTPException:
        inbound_ready = False
    return {
        "can_view_route": manager,
        "can_manage_route": manager
        and user.id in service.settings.clinic_operator_user_ids,
        "can_call": manager,
        "inbound_ready": inbound_ready,
        "outbound_ready": inbound_ready and bool(service.settings.exotel_caller_id),
    }


@router.get("/route", response_model=PhoneRouteOut)
async def get_route(
    org_id: UUID, bot_id: UUID, db: DB, user: CurrentUser, request: Request
):
    await clinic_bot(db, org_id, bot_id, user.id, write=True)
    route = await db.scalar(
        select(ClinicPhoneRoute).where(ClinicPhoneRoute.bot_id == bot_id)
    )
    if not route:
        raise HTTPException(404, "No inbound number configured")
    result = PhoneRouteOut.model_validate(route)
    if route.enabled and request.app.state.clinic_phone.settings.public_backend_url:
        result.inbound_stream_url = request.app.state.clinic_phone.inbound_url(route)
    return result


@router.put("/route", response_model=PhoneRouteOut)
async def set_route(
    org_id: UUID,
    bot_id: UUID,
    data: PhoneRouteConfig,
    db: DB,
    user: CurrentUser,
    request: Request,
):
    bot = await clinic_bot(db, org_id, bot_id, user.id, write=True)
    service = request.app.state.clinic_phone
    # The Exotel account is shared: only trusted operators can allocate its numbers.
    if user.id not in service.settings.clinic_operator_user_ids:
        raise HTTPException(403, "A platform operator must allocate the Exotel number")
    if data.enabled:
        service.require_ready()
        await bots.runtime_bot(db, bot)
    route = await db.scalar(
        select(ClinicPhoneRoute)
        .where(ClinicPhoneRoute.bot_id == bot_id)
        .with_for_update()
    )
    if route is None:
        route = ClinicPhoneRoute(bot_id=bot_id)
        db.add(route)
    route.phone_number, route.enabled, route.token_version = (
        data.phone_number,
        data.enabled,
        uuid4(),
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409, "This phone number already belongs to a clinic"
        ) from None
    result = PhoneRouteOut.model_validate(route)
    if route.enabled:
        result.inbound_stream_url = service.inbound_url(route)
    return result


@router.post("/calls", response_model=PhoneCallOut, status_code=202)
async def dial(
    org_id: UUID,
    bot_id: UUID,
    data: PhoneDial,
    db: DB,
    user: CurrentUser,
    request: Request,
):
    bot = await clinic_bot(db, org_id, bot_id, user.id, write=True)
    return await request.app.state.clinic_phone.dial(db, bot, data)


@router.get("/calls", response_model=list[PhoneCallOut])
async def calls(
    org_id: UUID,
    bot_id: UUID,
    db: DB,
    user: CurrentUser,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    await clinic_bot(db, org_id, bot_id, user.id)
    return list(
        await db.scalars(
            select(ClinicPhoneCall)
            .where(ClinicPhoneCall.bot_id == bot_id)
            .order_by(ClinicPhoneCall.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    )


@router.post("/calls/{call_id}/reconcile", response_model=PhoneCallOut)
async def reconcile(
    org_id: UUID,
    bot_id: UUID,
    call_id: UUID,
    data: PhoneReconcile,
    db: DB,
    user: CurrentUser,
    request: Request,
):
    await clinic_bot(db, org_id, bot_id, user.id, write=True)
    call = await db.scalar(
        select(ClinicPhoneCall)
        .where(ClinicPhoneCall.id == call_id, ClinicPhoneCall.bot_id == bot_id)
        .with_for_update()
    )
    if not call:
        raise HTTPException(404, "Call not found")
    if call.direction != "outbound" or call.status != "unknown":
        raise HTTPException(409, "Only an unknown outbound call needs reconciliation")
    service = request.app.state.clinic_phone
    if data.confirmed_not_placed:
        call.status, call.finished_at = "failed", utcnow()
        call.error = "Operator confirmed no call was placed"
        await db.commit()
    else:
        try:
            result = await service.provider.details(data.provider_call_sid)
            if (
                result["Sid"] != data.provider_call_sid
                or network_phone(result["From"]) != call.phone_number
            ):
                raise HTTPException(409, "Provider destination does not match")
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise HTTPException(502, "Unable to verify provider call") from None
        await db.commit()
        await service.apply_status(call_id, result["Sid"], result["Status"])
        await db.refresh(call)
    return call


@provider_router.post("/calls/{call_id}/status", status_code=204)
async def status(call_id: UUID, request: Request, token: str = ""):
    service = request.app.state.clinic_phone
    service.verify(call_id, "status", token)
    try:
        if "application/json" in request.headers.get("content-type", ""):
            payload = await request.json()
        else:
            payload = dict(
                await request.form(max_files=0, max_fields=50, max_part_size=16384)
            )
        await service.apply_status(
            call_id,
            payload.get("CallSid") or payload.get("Sid"),
            payload.get("Status") or payload.get("CallStatus"),
        )
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(400, "Invalid call status callback") from None
    return Response(status_code=204)


async def media(websocket, *, route_id=None, call_id=None, token=""):
    service = websocket.app.state.clinic_phone
    task = asyncio.current_task()
    admitted = False
    try:
        service.require_ready()
        if service.closing or len(service.live) >= constants.MAX_VOICE_SESSIONS:
            raise HTTPException(503, "Clinic phone capacity reached")
        if route_id:
            async with service.sessions() as db:
                route = await db.get(ClinicPhoneRoute, route_id)
                if not route or not route.enabled:
                    raise HTTPException(403, "Inbound route unavailable")
                service.verify(route.token_version, "inbound", token)
        else:
            service.verify(call_id, "media", token)
        # Count pending starts as well as active audio sessions.
        if service.closing or len(service.live) >= constants.MAX_VOICE_SESSIONS:
            raise HTTPException(503, "Clinic phone capacity reached")
        service.live.add(task)
        await websocket.accept()
        async with asyncio.timeout(15):
            event = await websocket.receive_json()
            if event.get("event") == "connected":
                event = await websocket.receive_json()
            start = service.validate_start(event)
            call, runtime = await service.admit(
                start, route_id=route_id, call_id=call_id, token=token
            )
        admitted = True
        from app.ai.clinic.pipeline import build_pipeline
        from app.ai.common.exotel import build_transport

        transport = build_transport(websocket, start)
        runner, worker = build_pipeline(
            None,
            runtime,
            service.settings,
            transport=transport,
            output_sample_rate=int(start["media_format"]["sample_rate"]),
            session_factory=service.sessions,
            direction=call.direction,
        )
        await runner.add_workers(worker)
        try:
            async with asyncio.timeout(constants.VOICE_SESSION_SECONDS):
                await runner.run()
        finally:
            await runner.cancel()
        # Media termination isn't authoritative telephony status: the provider polling
        # loop (and outbound callbacks) owns completion, including across restarts.
    except (
        HTTPException,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        TimeoutError,
        IntegrityError,
        httpx.HTTPError,
        WebSocketDisconnect,
    ):
        pass
    finally:
        service.live.discard(task)
        if websocket.client_state.name != "DISCONNECTED":
            with suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(code=1000 if admitted else 1008)


@provider_router.websocket("/inbound/{route_id}")
async def inbound_media(websocket: WebSocket, route_id: UUID, token: str = ""):
    await media(websocket, route_id=route_id, token=token)


@provider_router.websocket("/calls/{call_id}/media")
async def outbound_media(websocket: WebSocket, call_id: UUID, token: str = ""):
    await media(websocket, call_id=call_id, token=token)
