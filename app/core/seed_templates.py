"""Restore missing platform templates: uv run python -m app.core.seed_templates."""

import asyncio

from sqlalchemy import or_, select

from app.constants.constants import DEFAULT_TEMPLATES
from app.db.models import BotTemplate
from app.db.session import SessionFactory, engine


async def seed_missing_templates(db):
    existing = (
        await db.scalars(
            select(BotTemplate).where(
                or_(
                    BotTemplate.id.in_([data["id"] for data in DEFAULT_TEMPLATES]),
                    BotTemplate.slug.in_([data["slug"] for data in DEFAULT_TEMPLATES]),
                )
            )
        )
    ).all()
    ids, slugs = {t.id for t in existing}, {t.slug for t in existing}
    created = []
    for data in DEFAULT_TEMPLATES:
        if data["id"] in ids or data["slug"] in slugs:
            continue
        db.add(BotTemplate(**data, is_active=True))
        created.append(data["name"])
    await db.commit()
    return created


async def main():
    try:
        async with SessionFactory() as db:
            created = await seed_missing_templates(db)
            print(
                "Restored templates: " + ", ".join(created)
                if created
                else "Templates already exist."
            )
            rows = (
                await db.execute(
                    select(BotTemplate.name, BotTemplate.service_type).order_by(BotTemplate.name)
                )
            ).all()
            for name, service_type in rows:
                print(f"{name}: {service_type}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
