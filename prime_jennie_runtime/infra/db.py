"""Async SQLAlchemy 엔진 + 세션 팩토리.

asyncpg 드라이버 기반. Postgres 16 전용.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import PostgresConfig


def create_engine(config: PostgresConfig) -> AsyncEngine:
    """비동기 엔진 생성."""
    return create_async_engine(
        config.dsn,
        pool_size=5,
        max_overflow=10,
        echo=False,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """세션 팩토리 생성."""
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def get_session(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    """세션 컨텍스트 매니저. 커밋/롤백 자동 처리."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
