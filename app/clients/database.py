"""Database engine construction; the application owns engine disposal."""

from sqlalchemy.ext.asyncio import create_async_engine


def create_database_engine(settings):
    return create_async_engine(settings.database_url, pool_pre_ping=True)
