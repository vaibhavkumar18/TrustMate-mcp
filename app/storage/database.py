from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


def create_engine(settings: Settings) -> AsyncEngine | None:
    url = settings.sqlalchemy_url()
    if not url:
        return None
    return create_async_engine(
        url,
        connect_args={"ssl": True},
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_recycle=1800,
    )


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def session_dependency(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        yield session


async def check_database(engine: AsyncEngine | None) -> bool:
    if engine is None:
        return False
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    return True
