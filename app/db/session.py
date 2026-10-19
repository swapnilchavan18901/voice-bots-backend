from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clients import create_database_engine
from app.env import get_settings

engine = create_database_engine(get_settings())
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def get_db():
    async with SessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
