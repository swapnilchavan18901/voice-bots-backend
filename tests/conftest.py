import os

os.environ["JWT_SECRET"] = "test-secret-only-not-for-production-1234567890"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"
os.environ["SARVAM_API_KEY"] = ""

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotTemplate
from app.db.session import get_db
from app.main import app, public_app


@pytest_asyncio.fixture
async def client():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async with sessions() as db:
        from uuid import UUID

        db.add(
            BotTemplate(
                id=UUID("00000000-0000-4000-8000-000000000002"),
                slug="clinic-booking",
                name="Clinic",
                description="Appointment requests",
                system_prompt="Platform-owned clinic instructions",
                is_active=True,
            )
        )
        await db.commit()

    async def db_override():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = db_override
    public_app.dependency_overrides[get_db] = db_override
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, sessions
    app.dependency_overrides.clear()
    public_app.dependency_overrides.clear()
    await engine.dispose()
