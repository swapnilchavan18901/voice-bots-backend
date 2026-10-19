from uuid import UUID

from sqlalchemy import select

from app.core.seed_templates import seed_missing_templates
from app.db.models import BotTemplate


async def test_restore_defaults_preserves_existing_templates_and_is_repeatable(client):
    _, sessions = client
    async with sessions() as db:
        clinic = await db.get(BotTemplate, UUID("00000000-0000-4000-8000-000000000002"))
        clinic.name = "Customized clinic"
        clinic.system_prompt = "Keep these operator instructions"
        clinic.is_active = False
        await db.commit()
        assert len(await seed_missing_templates(db)) == 2
        assert await seed_missing_templates(db) == []
        assert clinic.name == "Customized clinic"
        assert clinic.system_prompt == "Keep these operator instructions"
        assert clinic.is_active is False
        assert len((await db.scalars(select(BotTemplate))).all()) == 3
