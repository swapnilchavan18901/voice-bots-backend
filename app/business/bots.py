from fastapi import HTTPException
from sqlalchemy import select

from app.business.organizations import MANAGE_ROLES, require_membership
from app.db.models import Bot, BotTemplate


async def list_bots(db, org_id, user_id):
    await require_membership(db, org_id, user_id)
    return (await db.scalars(select(Bot).where(Bot.organization_id == org_id))).all()


async def create_bot(db, org_id, user_id, data):
    await require_membership(db, org_id, user_id, MANAGE_ROLES)
    template = await active_template(db, data.template_id)
    bot = Bot(organization_id=org_id, **data.model_dump())
    db.add(bot)
    if template.service_type == "clinic":
        from app.business.clinic_context import sync_doctors

        await db.flush()
        await sync_doctors(db, bot)
    await db.commit()
    return bot


async def get_bot(db, org_id, bot_id, user_id, write=False):
    await require_membership(db, org_id, user_id, MANAGE_ROLES if write else None)
    query = select(Bot).where(Bot.id == bot_id, Bot.organization_id == org_id)
    if write:
        query = query.with_for_update()
    bot = await db.scalar(query)
    if not bot:
        raise HTTPException(404, "Bot not found")
    return bot


async def update_bot(db, org_id, bot_id, user_id, data):
    bot = await get_bot(db, org_id, bot_id, user_id, write=True)
    for key, value in data.model_dump().items():
        setattr(bot, key, value)
    template = await db.get(BotTemplate, bot.template_id)
    if template.service_type == "clinic":
        from app.business.clinic_context import sync_doctors

        await sync_doctors(db, bot)
    await db.commit()
    return bot


async def delete_bot(db, org_id, bot_id, user_id):
    bot = await get_bot(db, org_id, bot_id, user_id, write=True)
    await db.delete(bot)
    await db.commit()


async def active_template(db, template_id):
    template = await db.scalar(
        select(BotTemplate).where(BotTemplate.id == template_id, BotTemplate.is_active.is_(True))
    )
    if not template:
        raise HTTPException(404, "Bot template is unavailable")
    return template


async def list_templates(db):
    return (
        await db.scalars(
            select(BotTemplate).where(BotTemplate.is_active.is_(True)).order_by(BotTemplate.name)
        )
    ).all()


async def runtime_bot(db, bot):
    from types import SimpleNamespace

    template = await active_template(db, bot.template_id)
    if template.service_type == "clinic":
        from app.business.clinic_context import sync_doctors

        await db.execute(select(Bot).where(Bot.id == bot.id).with_for_update())
        await db.refresh(bot)
        await sync_doctors(db, bot)
        await db.commit()
    clinic_facts = None
    if template.service_type == "clinic":
        from app.business.clinic import clinic_info

        clinic_facts = await clinic_info(db, bot.id)
    # Keep the platform instructions and customer context in separate LLM messages.
    return SimpleNamespace(
        clinic_facts=clinic_facts,
        id=bot.id,
        organization_id=bot.organization_id,
        language=bot.language,
        name=bot.name,
        template_name=template.name,
        service_type=template.service_type,
        system_prompt=template.system_prompt,
        company_context=bot.company_context,
    )
