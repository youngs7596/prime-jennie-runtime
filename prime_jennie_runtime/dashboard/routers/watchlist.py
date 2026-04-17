"""Watchlist API — Redis 현재 + position_sheets 히스토리.

v2 원본: `prime_jennie/services/dashboard/routers/watchlist.py`

v3 매핑:
- 현재 워치리스트: Redis `watchlist:active` JSON (slow_loop 발행)
- 히스토리: v3 는 별도 watchlist 테이블이 없으므로 최근 `position_sheets` 로 대체.
  sheet 는 scout_run 당 복수 ticker 가 생성되므로 watchlist 의 의미를 충족.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import get_redis, get_session

router = APIRouter(prefix="/watchlist", tags=["watchlist"])

logger = logging.getLogger(__name__)

WATCHLIST_CACHE_KEY = "watchlist:active"


class WatchlistEntry(BaseModel):
    sheet_id: str
    ticker: str
    strategy_tag: str
    generated_at: datetime
    valid_until: datetime


@router.get("/current")
async def get_current(redis_client: aioredis.Redis = Depends(get_redis)) -> dict:
    """Redis 현재 워치리스트. 없으면 `{status: "no_data"}`."""
    try:
        raw = await redis_client.get(WATCHLIST_CACHE_KEY)
        if raw:
            return json.loads(raw)
    except Exception:
        logger.warning("watchlist current fetch failed", exc_info=True)
    return {"status": "no_data", "stocks": []}


@router.get("/history", response_model=list[WatchlistEntry])
async def get_history(
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
) -> list[WatchlistEntry]:
    """최근 발행된 position_sheets 를 watchlist 히스토리로 노출."""
    result = await session.execute(
        text(
            "SELECT sheet_id, ticker, strategy_tag, generated_at, valid_until "
            "FROM position_sheets ORDER BY generated_at DESC LIMIT :limit"
        ),
        {"limit": max(1, min(limit, 500))},
    )
    return [WatchlistEntry(**dict(r)) for r in result.mappings().all()]
