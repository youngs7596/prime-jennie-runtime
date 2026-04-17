"""유지보수 job — 데이터 정리 / 섹터 갱신 / 마스터 시드 / contract smoke.

v2 원본:
- `/jobs/cleanup-old-data` (app.py:2337-2350)
- `/jobs/update-naver-sectors` (app.py:2353-2373)
- `/jobs/seed-stock-masters` (app.py:2719-2742)
- `/jobs/contract-smoke-test` (app.py:2745-2886)

v3 어댑터: async + asyncpg. 결과는 로깅 / metric 으로만 드러내고 반환값 없음
(scheduler runner 가 `last_status` 를 기록).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)


DEFAULT_CLEANUP_DAYS = 365


async def cleanup_old_data(
    pool: Any,
    *,
    days: int = DEFAULT_CLEANUP_DAYS,
) -> None:
    """v2 `/jobs/cleanup-old-data` 포팅.

    v2 는 `stock_daily_prices` 를 365 일 기준으로 청소. v3 에서는 동일 로직을
    `daily_prices` (003) 에 적용한다. v2 가 날짜만 기준으로 삼았듯 v3 도
    `price_date < cutoff` 단일 조건만 사용.
    """
    cutoff = date.today() - timedelta(days=days)
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM daily_prices WHERE price_date < $1",
            cutoff,
        )
    deleted = 0
    if isinstance(result, str) and result.startswith("DELETE "):
        try:
            deleted = int(result.split(" ", 1)[1])
        except (IndexError, ValueError):
            deleted = 0
    logger.info("cleanup_old_data: cutoff=%s deleted=%d", cutoff.isoformat(), deleted)


__all__ = ["DEFAULT_CLEANUP_DAYS", "cleanup_old_data"]
