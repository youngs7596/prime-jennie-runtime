"""KOSPI/KOSDAQ 일봉 자동 수집 (시총 상위 300종목).

v2 원본: `/jobs/collect-full-market-data` (services/jobs/app.py:166-240).

v3 어댑터:
- v2 KIS 직접 호출 → kis_gateway HTTP `/api/market/daily-prices`
  (price_scheduler 와 동일 endpoint).
- 18 req/s 페이싱 (asyncio.sleep) 동일.
- v2 in-line INSERT 검사 → PostgresPriceRepo.upsert_daily (executemany 일괄).
- universe = stock_masters top300 by market_cap (price_scheduler.collect_daily
  의 universe-driven 구현과 별개 — 이건 자동 발견).
- 100건 단위 진행 로그는 단순 로그로 유지 (트랜잭션은 종목 단위 upsert 가
  내부적으로 자동 commit 되므로 v2 의 batch commit 의미와 다르지만 데이터
  일관성에는 영향 없음 — upsert 가 idempotent).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from prime_jennie_runtime.kis_gateway.price_repo import PostgresPriceRepo
from prime_jennie_runtime.kis_gateway.schemas import DailyPrice

logger = logging.getLogger(__name__)


_REQ_PER_SEC = 18
_MIN_INTERVAL = 1.0 / _REQ_PER_SEC
_PROGRESS_EVERY = 100


async def collect_full_market_data(
    pool: Any,
    http: httpx.AsyncClient,
    gateway_url: str,
    *,
    top_n: int = 300,
    days: int = 30,
) -> dict:
    """v2 `/jobs/collect-full-market-data` 포팅.

    1) stock_masters 활성 + 시총 desc 상위 N (ETN 제외, default 300)
    2) 각 종목 gateway `/api/market/daily-prices` POST → daily_prices upsert
    3) ~18 req/s 페이싱 (v2 와 동일)

    Returns: {"target": N, "upserted": N, "failed": N, "empty": N}
    """
    async with pool.acquire() as conn:
        # ETN 제외 — CD금리/레버리지 ETN 은 market_cap 상위를 점유하지만 KIS 일봉
        # endpoint 가 HTTP 200 + 빈 리스트를 줘 top_n 슬롯만 낭비한다 (2026-05-22
        # 발견: top300 중 131개가 ETN). ETF 는 일봉이 정상 수집되므로 유지.
        # stock_masters 에 instrument-type 컬럼이 없어 종목명 기반 필터 — 정식
        # 해법은 type 컬럼 추가.
        rows = await conn.fetch(
            "SELECT stock_code FROM stock_masters "
            "WHERE is_active = TRUE AND market_cap IS NOT NULL "
            "AND stock_name NOT ILIKE '%ETN%' "
            "ORDER BY market_cap DESC LIMIT $1",
            top_n,
        )
        targets = [r["stock_code"] for r in rows]
        logger.info(
            "collect_full_market_data: targets=%d (top%d by market_cap)",
            len(targets),
            top_n,
        )

        repo = PostgresPriceRepo(conn=conn)
        upserted = 0
        failed = 0
        empty = 0  # HTTP 200 인데 일봉 0건 — 예외가 아니라 failed 에 안 잡힘
        last_request = 0.0
        loop = asyncio.get_running_loop()

        for i, code in enumerate(targets):
            wait = last_request + _MIN_INTERVAL - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            last_request = loop.time()
            try:
                resp = await http.post(
                    f"{gateway_url}/api/market/daily-prices",
                    json={"stock_code": code, "days": days},
                    timeout=30.0,
                )
                resp.raise_for_status()
                items = [DailyPrice.model_validate(x) for x in resp.json()]
                if not items:
                    empty += 1
                upserted += await repo.upsert_daily(items)
            except Exception as e:
                failed += 1
                logger.warning("collect_full_market_data failed %s: %s", code, e)

            if (i + 1) % _PROGRESS_EVERY == 0:
                logger.info(
                    "collect_full_market_data: progress %d/%d upserted=%d failed=%d empty=%d",
                    i + 1,
                    len(targets),
                    upserted,
                    failed,
                    empty,
                )

    logger.info(
        "collect_full_market_data: target=%d upserted=%d failed=%d empty=%d",
        len(targets),
        upserted,
        failed,
        empty,
    )
    return {"target": len(targets), "upserted": upserted, "failed": failed, "empty": empty}


__all__ = ["collect_full_market_data"]
