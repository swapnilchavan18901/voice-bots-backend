from uuid import UUID

from fastapi import APIRouter

from app.api.dependencies import DB, CurrentUser
from app.business import organizations, workspace
from app.schemas.api import (
    InviteAccept,
    InviteCreate,
    InviteOut,
    MemberOut,
    OrgCreate,
    OrgOut,
    WorkspaceOut,
)

router = APIRouter(tags=["organizations"])


@router.get("/organizations", response_model=list[OrgOut])
async def list_orgs(db: DB, user: CurrentUser):
    return await organizations.list_organizations(db, user.id)


@router.post("/organizations", response_model=OrgOut, status_code=201)
async def create_org(data: OrgCreate, db: DB, user: CurrentUser):
    return await organizations.create_organization(db, user.id, data)


@router.get("/organizations/{org_id}/members", response_model=list[MemberOut])
async def members(org_id: UUID, db: DB, user: CurrentUser):
    return await organizations.list_members(db, org_id, user.id)


@router.post("/organizations/{org_id}/invitations", response_model=InviteOut, status_code=201)
async def invite(org_id: UUID, data: InviteCreate, db: DB, user: CurrentUser):
    return await organizations.invite(db, org_id, user.id, data)


@router.post("/invitations/accept", response_model=OrgOut)
async def accept(data: InviteAccept, db: DB, user: CurrentUser):
    return await organizations.accept_invitation(db, user, data.token)


@router.get("/organizations/{org_id}/workspace", response_model=WorkspaceOut)
async def workspace_overview(org_id: UUID, db: DB, user: CurrentUser):
    return await workspace.overview(db, org_id, user.id)
