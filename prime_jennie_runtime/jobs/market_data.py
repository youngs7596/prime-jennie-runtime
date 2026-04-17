"""시장 데이터 수집 job — 시총/지수/미국장/투자자 동향.

v2 원본:
- `/jobs/refresh-market-caps` (app.py:364-412)
- `/jobs/collect-index-daily-prices` (app.py:243-307)
- `/jobs/collect-us-market` (app.py:310-361)
- `/jobs/collect-investor-trading` (app.py:415-486)
- `/jobs/collect-foreign-holding` (app.py:489-550)

v3 어댑터: 모두 async. KIS snapshot 은 `kis_gateway` HTTP 엔드포인트 (`/api/snapshot/{ticker}`)
로 위임한다 — v2 의 직접 KISApi 호출 경로는 쓰지 않는다. 결과는 로그 + metric.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# v2 원본 상수 (재튜닝 금지).
MARKET_CAP_TOP_N = 300
MARKET_CAP_RATE_PER_SEC = 18.0
MARKET_CAP_BATCH_COMMIT_EVERY = 100


async def refresh_market_caps(
    pool: Any,
    http: httpx.AsyncClient,
    gateway_url: str,
    *,
    top_n: int = MARKET_CAP_TOP_N,
    rate_per_sec: float = MARKET_CAP_RATE_PER_SEC,
) -> None:
    """v2 `/jobs/refresh-market-caps` 포팅.

    시가총액 상위 `top_n` 활성 종목에 대해 KIS snapshot 을 받아 `stock_masters.market_cap`
    을 갱신. rate limit 은 v2 원문 그대로 18 req/sec (min_interval=1/18s).

    gateway_url 은 kis_gateway 의 base URL (예: `http://kis-gateway:8080`). snapshot
    엔드포인트는 `{gateway_url}/api/snapshot/{ticker}`. 응답이 400/500 이거나
    `market_cap` 이 0 이하면 skip.
    """
    min_interval = 1.0 / rate_per_sec
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT stock_code FROM stock_masters WHERE is_active = TRUE "
            "ORDER BY market_cap DESC NULLS LAST LIMIT $1",
            top_n,
        )
    codes = [r["stock_code"] for r in rows]
    logger.info("refresh_market_caps: candidates=%d", len(codes))

    updated = 0
    failed = 0
    last_request = 0.0

    async with pool.acquire() as conn:
        for i, code in enumerate(codes):
            now = time.monotonic()
            wait = last_request + min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            last_request = time.monotonic()

            try:
                resp = await http.get(
                    f"{gateway_url}/api/snapshot/{code}", timeout=10.0
                )
                resp.raise_for_status()
                snap = resp.json()
                market_cap = snap.get("market_cap")
                if market_cap and market_cap > 0:
                    await conn.execute(
                        "UPDATE stock_masters SET market_cap=$1, updated_at=NOW() "
                        "WHERE stock_code=$2",
                        market_cap,
                        code,
                    )
                    updated += 1
            except Exception as e:
                failed += 1
                logger.warning("market cap failed %s: %s", code, e)

            if (i + 1) % MARKET_CAP_BATCH_COMMIT_EVERY == 0:
                logger.info(
                    "market cap progress: %d/%d updated=%d",
                    i + 1,
                    len(codes),
                    updated,
                )

    logger.info(
        "refresh_market_caps: updated=%d failed=%d of %d",
        updated,
        failed,
        len(codes),
    )


__all__ = [
    "MARKET_CAP_BATCH_COMMIT_EVERY",
    "MARKET_CAP_RATE_PER_SEC",
    "MARKET_CAP_TOP_N",
    "refresh_market_caps",
]
