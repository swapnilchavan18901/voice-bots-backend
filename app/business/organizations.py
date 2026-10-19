
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.constants.constants import MANAGE_ROLES
from app.db.models import Invitation, Membership, Organization, User, utcnow
from app.types import InviteOut
from app.utils.security import opaque_token, token_digest


async def require_membership(db, org_id, user_id, roles=None):
    member = await db.scalar(
        select(Membership).where(
            Membership.organization_id == org_id, Membership.user_id == user_id
        )
    )
    if not member:
        raise HTTPException(404, "Organization not found")
    if roles and member.role not in roles:
        raise HTTPException(403, "Insufficient organization permissions")
    return member


async def list_organizations(db, user_id):
    return (
        await db.scalars(
            select(Organization)
            .join(Membership)
            .where(Membership.user_id == user_id)
            .order_by(Organization.created_at)
        )
    ).all()


async def create_organization(db, user_id, data):
    org = Organization(name=data.name)
    db.add(org)
    await db.flush()
    db.add(Membership(organization_id=org.id, user_id=user_id, role="owner"))
    await db.commit()
    return org


async def list_members(db, org_id, user_id):
    await require_membership(db, org_id, user_id)
    return (await db.scalars(select(Membership).where(Membership.organization_id == org_id))).all()


async def invite(db, org_id, user_id, data):
    member = await require_membership(db, org_id, user_id, MANAGE_ROLES)
    if data.role == "admin" and member.role != "owner":
        raise HTTPException(403, "Only owners can invite admins")
    existing = await db.scalar(
        select(Membership)
        .join(User)
        .where(Membership.organization_id == org_id, User.email == data.email)
    )
    if existing:
        raise HTTPException(409, "User is already a member")
    token = opaque_token()
    invitation = Invitation(
        organization_id=org_id,
        email=data.email,
        role=data.role,
        token_hash=token_digest(token),
        invited_by=user_id,
        expires_at=utcnow() + timedelta(days=7),
    )
    db.add(invitation)
    await db.commit()
    return InviteOut(id=invitation.id, token=token, expires_at=invitation.expires_at)


async def accept_invitation(db, user, token):
    invitation = await db.scalar(
        select(Invitation)
        .where(
            Invitation.token_hash == token_digest(token),
            Invitation.accepted_at.is_(None),
            Invitation.expires_at > utcnow(),
        )
        .with_for_update()
    )
    if not invitation:
        raise HTTPException(400, "Invitation is invalid, expired, or already accepted")
    if invitation.email != user.email:
        raise HTTPException(403, "Invitation belongs to another email address")
    member = await db.scalar(
        select(Membership).where(
            Membership.organization_id == invitation.organization_id, Membership.user_id == user.id
        )
    )
    if not member:
        db.add(
            Membership(
                organization_id=invitation.organization_id, user_id=user.id, role=invitation.role
            )
        )
    invitation.accepted_at = utcnow()
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Membership was updated; retry invitation acceptance") from None
    return await db.get(Organization, invitation.organization_id)
