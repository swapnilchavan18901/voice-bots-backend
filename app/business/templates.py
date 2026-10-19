"""Operator-only template management, invoked by CLI; never exposed to tenants."""

from sqlalchemy import select

from app.db.models import BotTemplate


async def upsert_template(db, data):
    template = await db.scalar(select(BotTemplate).where(BotTemplate.slug == data.slug))
    if template is None:
        template = BotTemplate(**data.model_dump(exclude_none=True))
        db.add(template)
    else:
        for key, value in data.model_dump(exclude_none=True).items():
            setattr(template, key, value)
    await db.commit()
    return template
