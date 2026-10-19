from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select

from app.business.organizations import require_membership
from app.constants.constants import MANAGE_ROLES
from app.db.models import ApiKey, utcnow
from app.types import ApiKeyCreated, ApiKeyOut
from app.utils.security import opaque_token, token_digest


async def create_key(db, org_id, user_id, data):
    await require_membership(db, org_id, user_id, MANAGE_ROLES)
    raw = "vb_" + opaque_token()
    key = ApiKey(
        organization_id=org_id,
        created_by=user_id,
        name=data.name,
        prefix=raw[:12],
        token_hash=token_digest(raw),
        expires_at=utcnow() + timedelta(days=data.expires_in_days),
    )
    db.add(key)
    await db.commit()
    return ApiKeyCreated(**ApiKeyOut.model_validate(key).model_dump(), key=raw)


async def list_keys(db, org_id, user_id):
    await require_membership(db, org_id, user_id, MANAGE_ROLES)
    return (await db.scalars(select(ApiKey).where(ApiKey.organization_id == org_id))).all()


async def revoke_key(db, org_id, user_id, key_id):
    await require_membership(db, org_id, user_id, MANAGE_ROLES)
    key = await db.scalar(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.organization_id == org_id)
    )
    if not key:
        raise HTTPException(404, "API key not found")
    key.revoked_at = utcnow()
    await db.commit()


async def authenticate_key(db, raw):
    if not raw or len(raw) > 200:
        raise HTTPException(401, "Valid API key required")
    key = await db.scalar(
        select(ApiKey).where(
            ApiKey.token_hash == token_digest(raw),
            ApiKey.revoked_at.is_(None),
            ApiKey.expires_at > utcnow(),
        )
    )
    if not key:
        raise HTTPException(401, "Invalid, expired, or revoked API key")
    return key
