"""Dashboard FastAPI dependencies — engine/redis 를 app.state 에 보관하고 라우터에 주입.

v2 `prime_jennie/services/deps.py` 의 `get_db_session` / `get_redis_client` 동등물.
async 전면 전환: `AsyncSession` + `redis.asyncio.Redis`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import redis.asyncio as aioredis
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def get_engine(request: Request) -> AsyncEngine:
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise RuntimeError("dashboard app.state.engine not initialised")
    return engine


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise RuntimeError("dashboard app.state.session_factory not initialised")
    return factory


async def get_session(
    factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> AsyncGenerator[AsyncSession, None]:
    async with factory() as session:
        yield session


def get_redis(request: Request) -> aioredis.Redis:
    client = getattr(request.app.state, "redis", None)
    if client is None:
        raise RuntimeError("dashboard app.state.redis not initialised")
    return client
