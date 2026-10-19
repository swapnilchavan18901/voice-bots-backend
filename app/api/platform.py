from uuid import UUID

from fastapi import APIRouter, Response

from app.api.dependencies import DB, CurrentUser
from app.business import api_keys, bots
from app.types import ApiKeyCreate, ApiKeyCreated, ApiKeyOut, TemplateOut

router = APIRouter(tags=["platform"])


@router.get("/bot-templates", response_model=list[TemplateOut])
async def templates(db: DB, user: CurrentUser):
    return await bots.list_templates(db)


@router.post("/organizations/{org_id}/api-keys", response_model=ApiKeyCreated, status_code=201)
async def create_key(org_id: UUID, data: ApiKeyCreate, db: DB, user: CurrentUser):
    return await api_keys.create_key(db, org_id, user.id, data)


@router.get("/organizations/{org_id}/api-keys", response_model=list[ApiKeyOut])
async def list_keys(org_id: UUID, db: DB, user: CurrentUser):
    return await api_keys.list_keys(db, org_id, user.id)


@router.delete("/organizations/{org_id}/api-keys/{key_id}", status_code=204)
async def revoke_key(org_id: UUID, key_id: UUID, db: DB, user: CurrentUser):
    await api_keys.revoke_key(db, org_id, user.id, key_id)
    return Response(status_code=204)
