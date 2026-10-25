from uuid import UUID

from fastapi import APIRouter, Request, Response

from app.api.dependencies import DB, CurrentUser
from app.business import bots
from app.types import Answer, BotConfig, BotCreate, BotOut, Offer, VoiceIceConfig

router = APIRouter(prefix="/organizations/{org_id}/bots", tags=["bots"])


@router.get("", response_model=list[BotOut])
async def list_bots(org_id: UUID, db: DB, user: CurrentUser):
    return await bots.list_bots(db, org_id, user.id)


@router.post("", response_model=BotOut, status_code=201)
async def create_bot(org_id: UUID, data: BotCreate, db: DB, user: CurrentUser):
    return await bots.create_bot(db, org_id, user.id, data)


@router.get("/{bot_id}", response_model=BotOut)
async def get_bot(org_id: UUID, bot_id: UUID, db: DB, user: CurrentUser):
    return await bots.get_bot(db, org_id, bot_id, user.id)


@router.put("/{bot_id}", response_model=BotOut)
async def update_bot(org_id: UUID, bot_id: UUID, data: BotConfig, db: DB, user: CurrentUser):
    return await bots.update_bot(db, org_id, bot_id, user.id, data)


@router.delete("/{bot_id}", status_code=204)
async def delete_bot(org_id: UUID, bot_id: UUID, db: DB, user: CurrentUser):
    await bots.delete_bot(db, org_id, bot_id, user.id)
    return Response(status_code=204)


@router.get(
    "/{bot_id}/voice/ice-servers",
    response_model=VoiceIceConfig,
    response_model_exclude_none=True,
)
async def ice_servers(
    org_id: UUID,
    bot_id: UUID,
    request: Request,
    response: Response,
    db: DB,
    user: CurrentUser,
):
    await bots.get_bot(db, org_id, bot_id, user.id)
    request.app.state.turn.throttle(user.id)
    response.headers["Cache-Control"] = "no-store"
    return await request.app.state.turn.configuration()


@router.post("/{bot_id}/voice/offer", response_model=Answer)
async def offer(
    org_id: UUID, bot_id: UUID, data: Offer, request: Request, db: DB, user: CurrentUser
):
    bot = await bots.get_bot(db, org_id, bot_id, user.id)
    return await request.app.state.voice.offer(await bots.runtime_bot(db, bot), user.id, data)


@router.delete("/{bot_id}/voice/{pc_id}", status_code=204)
async def stop_voice(
    org_id: UUID, bot_id: UUID, pc_id: str, request: Request, db: DB, user: CurrentUser
):
    await bots.get_bot(db, org_id, bot_id, user.id)
    await request.app.state.voice.stop(pc_id, user.id, org_id, bot_id)
    return Response(status_code=204)
