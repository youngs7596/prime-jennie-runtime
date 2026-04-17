"""투자자 동향 (외국인/기관 순매수 + 외국인 보유율) 수집 jobs.

v2 원본:
- `/jobs/collect-investor-trading` (app.py:415-486)
- `/jobs/collect-foreign-holding` (app.py:489-550)

두 job 모두 시총 상위 300종목을 대상으로 네이버 금융 frgn.naver 페이지를 1회 호출.
v2 는 `time.sleep(0.3)` 으로 throttle 하지만 v3 는 `asyncio.sleep` 로 동등.
실패는 카운트만 올리고 다음 종목으로 진행 — v2 와 동일.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any

import httpx

from .crawlers.naver_stock import fetch_stock_frgn_data

logger = logging.getLogger(__name__)


# v2 원문 상수 (재튜닝 금지).
INVESTOR_TOP_N = 300
INVESTOR_THROTTLE_SEC = 0.3
INVESTOR_RECENT_DAYS = 14  # cutoff = today - 14
INVESTOR_RECENT_LIMIT = 7  # 최근 7거래일 누적
INVESTOR_BATCH_PROGRESS_EVERY = 100


async def _top_active_stocks(pool: Any, top_n: int) -> list[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT stock_code FROM stock_masters WHERE is_active = TRUE "
            "ORDER BY market_cap DESC NULLS LAST LIMIT $1",
            top_n,
        )
    return [r["stock_code"] for r in rows]


async def collect_investor_trading(
    pool: Any,
    http: httpx.AsyncClient,
    *,
    top_n: int = INVESTOR_TOP_N,
    throttle_sec: float = INVESTOR_THROTTLE_SEC,
) -> None:
    """v2 `/jobs/collect-investor-trading` 포팅.

    네이버 frgn 페이지에서 종목별 일자별 외국인/기관 순매매량 + 종가를 받아
    최근 7거래일 누적 (volume × close_price = KRW) 을 `stock_investor_tradings`
    의 trade_date=오늘 row 로 upsert. v2 와 동일하게 cutoff = today - 14d.
    """
    codes = await _top_active_stocks(pool, top_n)
    logger.info("collect_investor_trading: candidates=%d", len(codes))
    cutoff = date.today() - timedelta(days=INVESTOR_RECENT_DAYS)
    today = date.today()

    collected = 0
    failed = 0

    async with pool.acquire() as conn:
        for i, code in enumerate(codes):
            if i > 0:
                await asyncio.sleep(throttle_sec)
            try:
                rows = await fetch_stock_frgn_data(http, code)
                if not rows:
                    failed += 1
                    continue

                recent = [r for r in rows if r.trade_date >= cutoff][:INVESTOR_RECENT_LIMIT]
                if not recent:
                    continue

                foreign_net = sum(r.frgn_net_volume * r.close_price for r in recent)
                inst_net = sum(r.inst_net_volume * r.close_price for r in recent)

                await conn.execute(
                    "INSERT INTO stock_investor_tradings "
                    "(stock_code, trade_date, foreign_net_buy, institution_net_buy) "
                    "VALUES ($1, $2, $3, $4) "
                    "ON CONFLICT (stock_code, trade_date) DO UPDATE SET "
                    "foreign_net_buy=EXCLUDED.foreign_net_buy, "
                    "institution_net_buy=EXCLUDED.institution_net_buy",
                    code,
                    today,
                    float(foreign_net),
                    float(inst_net),
                )
                collected += 1
            except Exception as e:
                failed += 1
                logger.warning("investor trading failed %s: %s", code, e)

            if (i + 1) % INVESTOR_BATCH_PROGRESS_EVERY == 0:
                logger.info(
                    "investor trading progress: %d/%d collected=%d",
                    i + 1,
                    len(codes),
                    collected,
                )

    logger.info(
        "collect_investor_trading: collected=%d failed=%d of %d",
        collected,
        failed,
        len(codes),
    )


async def collect_foreign_holding(
    pool: Any,
    http: httpx.AsyncClient,
    *,
    top_n: int = INVESTOR_TOP_N,
    throttle_sec: float = INVESTOR_THROTTLE_SEC,
) -> None:
    """v2 `/jobs/collect-foreign-holding` 포팅.

    네이버 frgn 페이지의 최신 row 의 trade_date + frgn_holding_ratio 를
    `stock_investor_tradings` 에 upsert. trade_date 는 v2 와 동일하게 크롤러가
    돌려준 latest row 의 날짜 (오늘이 아님).
    """
    codes = await _top_active_stocks(pool, top_n)
    logger.info("collect_foreign_holding: candidates=%d", len(codes))

    updated = 0
    failed = 0

    async with pool.acquire() as conn:
        for i, code in enumerate(codes):
            if i > 0:
                await asyncio.sleep(throttle_sec)
            try:
                rows = await fetch_stock_frgn_data(http, code)
                if not rows:
                    failed += 1
                    continue

                latest = rows[0]
                await conn.execute(
                    "INSERT INTO stock_investor_tradings "
                    "(stock_code, trade_date, foreign_holding_ratio) "
                    "VALUES ($1, $2, $3) "
                    "ON CONFLICT (stock_code, trade_date) DO UPDATE SET "
                    "foreign_holding_ratio=EXCLUDED.foreign_holding_ratio",
                    code,
                    latest.trade_date,
                    float(latest.frgn_holding_ratio),
                )
                updated += 1
            except Exception as e:
                failed += 1
                logger.warning("foreign holding failed %s: %s", code, e)

            if (i + 1) % INVESTOR_BATCH_PROGRESS_EVERY == 0:
                logger.info(
                    "foreign holding progress: %d/%d updated=%d",
                    i + 1,
                    len(codes),
                    updated,
                )

    logger.info(
        "collect_foreign_holding: updated=%d failed=%d of %d",
        updated,
        failed,
        len(codes),
    )


__all__ = [
    "INVESTOR_BATCH_PROGRESS_EVERY",
    "INVESTOR_RECENT_DAYS",
    "INVESTOR_RECENT_LIMIT",
    "INVESTOR_THROTTLE_SEC",
    "INVESTOR_TOP_N",
    "collect_foreign_holding",
    "collect_investor_trading",
]
