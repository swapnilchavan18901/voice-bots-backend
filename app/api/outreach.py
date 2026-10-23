"""Organization-scoped Outreach API plus narrowly authenticated Exotel callbacks."""

import asyncio
import json
from typing import Annotated, Literal
from urllib.parse import parse_qs
from uuid import UUID

import httpx
from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
)
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import select
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.websockets import WebSocketDisconnect

from app.api.dependencies import DB, CurrentUser
from app.business import outreach
from app.db.models import OutreachAgent, OutreachCall, OutreachCampaign, utcnow
from app.outreach_types import (
    MAX_CSV_BYTES,
    CampaignCreate,
    CampaignOut,
    Contact,
    OutreachAgentConfig,
    OutreachAgentOut,
    OutreachCallOut,
    parse_contacts_csv,
)

router = APIRouter(prefix="/organizations/{org_id}/outreach", tags=["Sia Outreach"])
provider_router = APIRouter(prefix="/outreach/exotel", tags=["Exotel callbacks"])


@router.get("/agents", response_model=list[OutreachAgentOut])
async def list_agents(org_id: UUID, db: DB, user: CurrentUser):
    await outreach.access(db, org_id, user.id)
    return (
        await db.scalars(
            select(OutreachAgent)
            .where(OutreachAgent.organization_id == org_id)
            .order_by(OutreachAgent.created_at)
        )
    ).all()


@router.post("/agents", response_model=OutreachAgentOut, status_code=201)
async def create_agent(
    org_id: UUID, data: OutreachAgentConfig, db: DB, user: CurrentUser
):
    await outreach.access(db, org_id, user.id, write=True)
    agent = OutreachAgent(organization_id=org_id, **data.model_dump())
    db.add(agent)
    await db.commit()
    return agent


@router.get("/agents/{agent_id}", response_model=OutreachAgentOut)
async def get_agent(org_id: UUID, agent_id: UUID, db: DB, user: CurrentUser):
    return await outreach.agent(db, org_id, agent_id, user.id)


@router.put("/agents/{agent_id}", response_model=OutreachAgentOut)
async def update_agent(
    org_id: UUID, agent_id: UUID, data: OutreachAgentConfig, db: DB, user: CurrentUser
):
    agent = await outreach.agent(db, org_id, agent_id, user.id, write=True)
    for key, value in data.model_dump().items():
        setattr(agent, key, value)
    await db.commit()
    return agent


@router.get("/contacts-template.csv")
async def contacts_template(org_id: UUID, db: DB, user: CurrentUser):
    await outreach.access(db, org_id, user.id)
    return Response(
        'phone_number,name,context\n+919876543210,Example Customer,"Interested in the standard plan"\n',
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="sia-outreach-contacts.csv"'
        },
    )


@router.post("/campaigns", response_model=CampaignOut, status_code=201)
async def create_campaign(
    org_id: UUID, data: CampaignCreate, db: DB, user: CurrentUser
):
    return await outreach.create_campaign(db, org_id, user.id, data)


@router.post("/campaigns/csv", response_model=CampaignOut, status_code=201)
async def create_csv_campaign(
    org_id: UUID,
    db: DB,
    user: CurrentUser,
    agent_id: Annotated[UUID, Form()],
    name: Annotated[str, Form(min_length=1, max_length=100)],
    file: Annotated[UploadFile, File()],
    mode: Annotated[Literal["sequential", "parallel"], Form()] = "sequential",
):
    await outreach.access(db, org_id, user.id, write=True)
    try:
        content = await file.read(MAX_CSV_BYTES + 1)
        contacts = parse_contacts_csv(content)
        data = CampaignCreate(
            name=name, agent_id=agent_id, mode=mode, contacts=contacts
        )
    except ValueError as exc:
        message = (
            "Invalid campaign or duplicate phone numbers"
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        raise HTTPException(422, message) from None
    finally:
        await file.close()
    return await outreach.create_campaign(db, org_id, user.id, data)


@router.get("/campaigns", response_model=list[CampaignOut])
async def list_campaigns(
    org_id: UUID,
    db: DB,
    user: CurrentUser,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    await outreach.access(db, org_id, user.id)
    return (
        await db.scalars(
            select(OutreachCampaign)
            .where(OutreachCampaign.organization_id == org_id)
            .order_by(OutreachCampaign.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()


@router.get("/campaigns/{campaign_id}", response_model=CampaignOut)
async def get_campaign(org_id: UUID, campaign_id: UUID, db: DB, user: CurrentUser):
    return await outreach.campaign(db, org_id, campaign_id, user.id)


@router.get("/campaigns/{campaign_id}/calls", response_model=list[OutreachCallOut])
async def list_calls(
    org_id: UUID,
    campaign_id: UUID,
    db: DB,
    user: CurrentUser,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
):
    await outreach.campaign(db, org_id, campaign_id, user.id)
    return (
        await db.scalars(
            select(OutreachCall)
            .where(OutreachCall.campaign_id == campaign_id)
            .order_by(OutreachCall.position)
            .offset(offset)
            .limit(limit)
        )
    ).all()


@router.post("/campaigns/{campaign_id}/run", response_model=CampaignOut)
async def run_campaign(
    org_id: UUID, campaign_id: UUID, request: Request, db: DB, user: CurrentUser
):
    return await outreach.run(
        db, org_id, campaign_id, user.id, request.app.state.outreach
    )


@router.post("/campaigns/{campaign_id}/stop", response_model=CampaignOut)
async def stop_campaign(org_id: UUID, campaign_id: UUID, db: DB, user: CurrentUser):
    """Cancel pending numbers; already reserved/active calls finish before the campaign stops."""
    return await outreach.stop(db, org_id, campaign_id, user.id)


class ReconcileCall(BaseModel):
    provider_call_sid: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )
    confirmed_not_placed: Literal[True] | None = None

    @model_validator(mode="after")
    def exactly_one(self):
        if bool(self.provider_call_sid) == bool(self.confirmed_not_placed):
            raise ValueError(
                "Provide a call SID or explicitly confirm Exotel never placed the call"
            )
        return self


@router.post(
    "/campaigns/{campaign_id}/calls/{call_id}/reconcile", response_model=OutreachCallOut
)
async def reconcile_call(
    org_id: UUID,
    campaign_id: UUID,
    call_id: UUID,
    data: ReconcileCall,
    request: Request,
    db: DB,
    user: CurrentUser,
):
    await outreach.campaign(db, org_id, campaign_id, user.id, write=True)
    call = await db.scalar(
        select(OutreachCall)
        .where(OutreachCall.id == call_id, OutreachCall.campaign_id == campaign_id)
        .with_for_update()
    )
    if not call:
        raise HTTPException(404, "Call not found")
    if call.status != "unknown":
        raise HTTPException(409, "Only an unknown dispatch needs reconciliation")
    if data.confirmed_not_placed:
        call.status = "failed"
        call.error = (
            "Organization operator confirmed that Exotel did not place the call"
        )
        call.finished_at = utcnow()
        await db.commit()
    else:
        try:
            result = await request.app.state.outreach.provider.details(
                data.provider_call_sid
            )
            if Contact(phone_number=result["From"]).phone_number != call.phone_number:
                raise HTTPException(
                    409, "Provider call destination does not match this contact"
                )
        except HTTPException:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise HTTPException(502, "Unable to verify provider call") from None
        await db.commit()  # Release locks before the service opens its own transaction.
        await request.app.state.outreach.apply_status(
            call_id, result["Sid"], result["Status"]
        )
        await db.refresh(call)
    return call


@provider_router.post("/{call_id}/status", status_code=204)
async def exotel_status(call_id: UUID, request: Request, token: str = ""):
    service = request.app.state.outreach
    service.verify(call_id, "status", token)
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > 16384:
            raise HTTPException(413, "Callback too large")
    try:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("application/json"):
            payload = json.loads(content)
        elif content_type.startswith("multipart/form-data"):

            async def stream():
                yield bytes(content)

            payload = dict(
                await MultiPartParser(
                    request.headers,
                    stream(),
                    max_files=0,
                    max_fields=20,
                    max_part_size=16384,
                ).parse()
            )
        else:
            payload = {k: v[-1] for k, v in parse_qs(content.decode()).items()}
        await service.apply_status(
            call_id,
            payload.get("CallSid") or payload.get("Sid"),
            payload.get("Status") or payload.get("CallStatus"),
        )
    except (ValueError, AttributeError, TypeError, MultiPartException):
        raise HTTPException(400, "Invalid status callback") from None
    return Response(status_code=204)


@provider_router.websocket("/{call_id}/media")
async def exotel_media(websocket: WebSocket, call_id: UUID, token: str = ""):
    service = websocket.app.state.outreach
    try:
        service.verify(call_id, "media", token)
        service.require_ready()
    except HTTPException:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    claimed = False
    try:
        async with asyncio.timeout(15):
            event = await websocket.receive_json()
            if event.get("event") == "connected":
                event = await websocket.receive_json()
            start = event["start"]
            audio_format = start["media_format"]
            if (
                event.get("event") != "start"
                or not start.get("stream_sid")
                or not start.get("call_sid")
                or start.get("account_sid") != service.settings.exotel_account_sid
                or audio_format.get("encoding") != "audio/x-raw"
                or int(audio_format.get("bit_rate", 16)) != 16
                or int(audio_format["sample_rate"]) not in {8000, 16000, 24000}
            ):
                raise ValueError("Invalid Exotel start event")
        async with service.sessions() as db:
            call = await db.scalar(
                select(OutreachCall).where(OutreachCall.id == call_id).with_for_update()
            )
            if (
                not call
                or call.status not in outreach.ACTIVE
                or call.stream_claimed
                or (
                    call.provider_call_sid
                    and call.provider_call_sid != start["call_sid"]
                )
            ):
                raise ValueError("Unavailable call")
            call.stream_claimed = True
            call.provider_call_sid = start["call_sid"]
            call.status = "in-progress"
            campaign = await db.get(OutreachCampaign, call.campaign_id)
            snapshot = campaign.agent_snapshot
            contact = {"name": call.name, "context": call.context}
            await db.commit()
            claimed = True
        from app.ai.outreach import run_voice

        async with asyncio.timeout(1800):
            await run_voice(websocket, snapshot, contact, start, service.settings)
    except (ValueError, KeyError, TypeError, TimeoutError, WebSocketDisconnect):
        pass
    finally:
        # A media disconnect does not prove the phone call ended. Only Exotel status
        # can release the batch slot. Prevent reconnects from creating a second voice agent.
        if websocket.client_state.name != "DISCONNECTED":
            try:
                await websocket.close(code=1000 if claimed else 1008)
            except RuntimeError:
                pass
