"""Server-prepared workspace data; clients do not derive access or feature scope."""

from sqlalchemy import select

from app.business.bots import list_templates
from app.business.organizations import require_membership
from app.constants.constants import MANAGE_ROLES
from app.db.models import Bot, BotTemplate, Membership, Organization


async def overview(db, org_id, user_id):
    membership = await require_membership(db, org_id, user_id)
    catalog = await list_templates(db)
    features = {name: {"bots": [], "templates": []} for name in ("clinic", "conversation")}
    for template in catalog:
        features[template.service_type]["templates"].append(template)
    rows = (
        await db.execute(
            select(Bot, BotTemplate.service_type, BotTemplate.is_active)
            .join(BotTemplate)
            .where(Bot.organization_id == org_id)
            .order_by(Bot.name, Bot.id)
        )
    ).all()
    for bot, feature, active in rows:
        if active:
            features[feature]["bots"].append(bot)
    members = (
        await db.scalars(select(Membership).where(Membership.organization_id == org_id))
    ).all()
    return {
        "organization": await db.get(Organization, org_id),
        "role": membership.role,
        "can_manage": membership.role in MANAGE_ROLES,
        "can_invite_admins": membership.role == "owner",
        "bots": [bot for bot, _, _ in rows],
        "templates": catalog,
        "features": features,
        "members": members,
    }
