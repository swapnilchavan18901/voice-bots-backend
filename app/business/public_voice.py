
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select, update

from app.business.bots import runtime_bot
from app.constants import constants
from app.db.models import ApiKey, Bot, VoiceGrant, utcnow
from app.types import VoiceGrantOut
from app.utils.security import opaque_token, token_digest


async def create_grant(db, key, bot_id):
    bot = await db.scalar(
        select(Bot).where(Bot.id == bot_id, Bot.organization_id == key.organization_id)
    )
    if not bot:
        raise HTTPException(404, "Bot not found")
    await runtime_bot(db, bot)
    raw = opaque_token()
    now = utcnow()
    grant = VoiceGrant(
        api_key_id=key.id,
        bot_id=bot.id,
        token_hash=token_digest(raw),
        offer_expires_at=now + timedelta(minutes=5),
        expires_at=now + timedelta(seconds=300 + constants.VOICE_SESSION_SECONDS),
    )
    db.add(grant)
    await db.commit()
    return VoiceGrantOut(
        token=raw,
        bot_id=bot.id,
        offer_expires_at=grant.offer_expires_at,
        expires_at=grant.expires_at,
    )


async def resolve_grant(db, bot_id, raw):
    if not raw or len(raw) > 200:
        raise HTTPException(401, "Voice session token required")
    row = (
        await db.execute(
            select(VoiceGrant, Bot)
            .join(Bot, Bot.id == VoiceGrant.bot_id)
            .join(ApiKey, ApiKey.id == VoiceGrant.api_key_id)
            .where(
                VoiceGrant.token_hash == token_digest(raw),
                VoiceGrant.bot_id == bot_id,
                VoiceGrant.expires_at > utcnow(),
                ApiKey.revoked_at.is_(None),
                ApiKey.expires_at > utcnow(),
                ApiKey.organization_id == Bot.organization_id,
            )
        )
    ).first()
    if not row:
        raise HTTPException(401, "Invalid or expired voice token or revoked API key")
    return row


async def start_voice(db, bot_id, raw, offer, manager):
    grant, bot = await resolve_grant(db, bot_id, raw)
    runtime = await runtime_bot(db, bot)
    # Atomic across workers: one token can start at most one call, even under concurrent requests.
    result = await db.execute(
        update(VoiceGrant)
        .where(
            VoiceGrant.id == grant.id,
            VoiceGrant.used_at.is_(None),
            VoiceGrant.offer_expires_at > utcnow(),
        )
        .values(used_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise HTTPException(409, "Voice token has already been used or its offer window expired")
    await db.commit()
    # Consumption is deliberate even on signaling failure; obtain a new token to retry.
    return await manager.offer(runtime, grant.id, offer)


async def stop_voice(db, bot_id, raw, pc_id, manager):
    grant, bot = await resolve_grant(db, bot_id, raw)
    await manager.stop(pc_id, grant.id, bot.organization_id, bot.id)
