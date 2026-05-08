"""5분봉 자동 수집 (시총 상위 30 + 워치리스트) — 백테스트용 보조 수집기.

v2 원본: `/jobs/collect-minute-chart` (services/jobs/app.py:624-699).

v3 어댑터:
- v2 KIS 직접 호출 (rate-limit time.sleep 1/18s) → kis_gateway HTTP
  `/api/market/minute-prices`. asyncio.sleep 으로 동일 ~18 req/s 페이싱.
- v2 in-line INSERT 검사 → PostgresPriceRepo.upsert_minute (executemany 일괄).
- v2 universe = StockMasterDB top30 by market_cap + 최신 watchlist_histories.
  v3 schema 동일 (009_v2_trading_ops.sql) → asyncpg query 로 재현.
- 2026-05-08 부터 KIS 분봉 단일 수집자. price_scheduler.collect_minute 은 5종목
  sample placeholder 였던 잔재 잡으로 obsolete (top30 안에 모두 포함되어 100% 중복).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from prime_jennie_runtime.kis_gateway.price_repo import PostgresPriceRepo
from prime_jennie_runtime.kis_gateway.schemas import MinutePrice

logger = logging.getLogger(__name__)


_REQ_PER_SEC = 18  # v2 1/18s sleep → KIS rate-limit 마진.
_MIN_INTERVAL = 1.0 / _REQ_PER_SEC


async def collect_minute_chart(
    pool: Any,
    http: httpx.AsyncClient,
    gateway_url: str,
    *,
    top_n: int = 30,
) -> dict:
    """v2 `/jobs/collect-minute-chart` 포팅.

    1) stock_masters 활성 + 시총 desc 상위 N (default 30)
    2) 최신 watchlist_histories.snapshot_date 의 stock_code 추가
    3) 각 종목 gateway `/api/market/minute-prices` POST → minute_prices upsert
    4) ~18 req/s 페이싱 (v2 와 동일)

    Returns: {"target": N, "top_n": N, "watchlist_added": N, "upserted": N, "failed": N}
    """
    async with pool.acquire() as conn:
        top_rows = await conn.fetch(
            "SELECT stock_code FROM stock_masters "
            "WHERE is_active = TRUE AND market_cap IS NOT NULL "
            "ORDER BY market_cap DESC LIMIT $1",
            top_n,
        )
        target_codes = {r["stock_code"] for r in top_rows}
        top_count = len(target_codes)

        latest_date = await conn.fetchval(
            "SELECT snapshot_date FROM watchlist_histories ORDER BY snapshot_date DESC LIMIT 1"
        )
        watchlist_added = 0
        if latest_date is not None:
            wl_rows = await conn.fetch(
                "SELECT stock_code FROM watchlist_histories "
                "WHERE snapshot_date = $1 AND is_active = TRUE",
                latest_date,
            )
            for r in wl_rows:
                if r["stock_code"] not in target_codes:
                    target_codes.add(r["stock_code"])
                    watchlist_added += 1

        target_list = sorted(target_codes)
        logger.info(
            "collect_minute_chart: targets=%d (top%d=%d + watchlist=%d)",
            len(target_list),
            top_n,
            top_count,
            watchlist_added,
        )

        repo = PostgresPriceRepo(conn=conn)
        upserted = 0
        failed = 0
        last_request = 0.0
        loop = asyncio.get_running_loop()

        for code in target_list:
            wait = last_request + _MIN_INTERVAL - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            last_request = loop.time()
            try:
                resp = await http.post(
                    f"{gateway_url}/api/market/minute-prices",
                    json={"stock_code": code},
                    timeout=30.0,
                )
                resp.raise_for_status()
                items = [MinutePrice.model_validate(x) for x in resp.json()]
                upserted += await repo.upsert_minute(items)
            except Exception as e:
                failed += 1
                logger.warning("collect_minute_chart failed %s: %s", code, e)

    logger.info(
        "collect_minute_chart: target=%d upserted=%d failed=%d",
        len(target_list),
        upserted,
        failed,
    )
    return {
        "target": len(target_list),
        "top_n": top_count,
        "watchlist_added": watchlist_added,
        "upserted": upserted,
        "failed": failed,
    }


__all__ = ["collect_minute_chart"]
