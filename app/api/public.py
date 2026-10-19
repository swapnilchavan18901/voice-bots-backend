from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.security import HTTPAuthorizationCredentials

from app.api.dependencies import DB, bearer
from app.business import api_keys, public_voice
from app.types import Answer, Offer, VoiceGrantOut

router = APIRouter(tags=["public voice"])
VoiceCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]


@router.post("/bots/{bot_id}/session-token", response_model=VoiceGrantOut, status_code=201)
async def session_token(
    bot_id: UUID, db: DB, api_key: Annotated[str | None, Header(alias="X-API-Key")] = None
):
    key = await api_keys.authenticate_key(db, api_key)
    return await public_voice.create_grant(db, key, bot_id)


@router.post("/bots/{bot_id}/webrtc/offer", response_model=Answer)
async def offer(bot_id: UUID, data: Offer, request: Request, db: DB, credentials: VoiceCredentials):
    return await public_voice.start_voice(
        db, bot_id, credentials.credentials if credentials else None, data, request.app.state.voice
    )


@router.delete("/bots/{bot_id}/webrtc/{pc_id}", status_code=204)
async def stop(bot_id: UUID, pc_id: str, request: Request, db: DB, credentials: VoiceCredentials):
    await public_voice.stop_voice(
        db, bot_id, credentials.credentials if credentials else None, pc_id, request.app.state.voice
    )
    return Response(status_code=204)
