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

from .crawlers.naver_market import fetch_index_daily_prices
from .crawlers.us_market import fetch_us_market_batch

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
                resp = await http.get(f"{gateway_url}/api/snapshot/{code}", timeout=10.0)
                resp.raise_for_status()
                snap = resp.json()
                market_cap = snap.get("market_cap")
                if market_cap and market_cap > 0:
                    # KIS hts_avls 는 억원 단위. stock_masters.market_cap 캐논은 백만원
                    # (네이버 시드의 cap_eok×100 과 같은 단위)이라 ×100 으로 맞춘다.
                    # 2026-06-26 정정: 이 ×100 누락으로 KIS 갱신 행이 100배 작게 들어가,
                    # 유니버스의 ORDER BY market_cap 순위가 네이버 백만원 행과 뒤집혔다
                    # (1.27조 ETN 이 103조 현대차 위에 랭크되는 라이브 사고).
                    market_cap_mwon = market_cap * 100
                    await conn.execute(
                        "UPDATE stock_masters SET market_cap=$1, updated_at=NOW() "
                        "WHERE stock_code=$2",
                        market_cap_mwon,
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


INDEX_DAILY_DEFAULT_DAYS = 250
INDEX_CODES: tuple[str, ...] = ("KOSPI", "KOSDAQ")


async def collect_index_daily_prices(
    pool: Any,
    http: httpx.AsyncClient,
    *,
    days: int = INDEX_DAILY_DEFAULT_DAYS,
) -> None:
    """v2 `/jobs/collect-index-daily-prices` 포팅 (app.py:243-307).

    KOSPI/KOSDAQ 지수 일봉 OHLCV 를 fchart API 로 가져와 `index_daily_prices` 에
    upsert. change_pct 는 v2 원문대로 인접 바 기준 (`(close - prev_close) / prev_close * 100`).

    크롤러(`fetch_index_daily_prices`) 는 이미 `price_date` 오름차순 정렬되어 반환됨.
    """
    total = 0
    async with pool.acquire() as conn:
        for index_code in INDEX_CODES:
            rows = await fetch_index_daily_prices(http, index_code, count=days)
            if not rows:
                logger.warning("no index daily data for %s", index_code)
                continue

            prev_close: float | None = None
            for row in rows:
                change_pct: float | None = None
                if prev_close is not None and prev_close > 0:
                    change_pct = (row.close_price - prev_close) / prev_close * 100
                prev_close = row.close_price

                await conn.execute(
                    "INSERT INTO index_daily_prices (index_code, price_date, "
                    "open_price, high_price, low_price, close_price, volume, change_pct) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
                    "ON CONFLICT (index_code, price_date) DO UPDATE SET "
                    "open_price=EXCLUDED.open_price, "
                    "high_price=EXCLUDED.high_price, "
                    "low_price=EXCLUDED.low_price, "
                    "close_price=EXCLUDED.close_price, "
                    "volume=EXCLUDED.volume, "
                    "change_pct=EXCLUDED.change_pct",
                    row.index_code,
                    row.price_date,
                    row.open_price,
                    row.high_price,
                    row.low_price,
                    row.close_price,
                    row.volume,
                    change_pct,
                )
                total += 1
            logger.info("index %s: %d rows upserted", index_code, len(rows))

    logger.info("collect_index_daily_prices: total=%d", total)


US_MARKET_DEFAULT_DAYS = 500


async def collect_us_market(
    pool: Any,
    http: httpx.AsyncClient,
    *,
    days: int = US_MARKET_DEFAULT_DAYS,
) -> None:
    """v2 `/jobs/collect-us-market` 포팅 (app.py:310-361).

    SOX / NVDA / S&P 500 / NASDAQ / NQ 선물 일봉을 Yahoo chart API 로 받아
    `us_market_daily` 에 upsert. change_pct 는 crawler 가 이미 계산 (round 4).
    """
    batch = await fetch_us_market_batch(http, days=days)
    total = 0
    async with pool.acquire() as conn:
        for ticker_name, rows in batch.items():
            for row in rows:
                await conn.execute(
                    "INSERT INTO us_market_daily (ticker, price_date, "
                    "open_price, high_price, low_price, close_price, volume, "
                    "change_pct) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
                    "ON CONFLICT (ticker, price_date) DO UPDATE SET "
                    "open_price=EXCLUDED.open_price, "
                    "high_price=EXCLUDED.high_price, "
                    "low_price=EXCLUDED.low_price, "
                    "close_price=EXCLUDED.close_price, "
                    "volume=EXCLUDED.volume, "
                    "change_pct=EXCLUDED.change_pct, "
                    "updated_at=NOW()",
                    row.ticker,
                    row.price_date,
                    row.open_price,
                    row.high_price,
                    row.low_price,
                    row.close_price,
                    row.volume,
                    row.change_pct,
                )
                total += 1
            logger.info("us_market %s: %d rows upserted", ticker_name, len(rows))
    logger.info("collect_us_market: total=%d tickers=%s", total, list(batch.keys()))


__all__ = [
    "INDEX_CODES",
    "INDEX_DAILY_DEFAULT_DAYS",
    "MARKET_CAP_BATCH_COMMIT_EVERY",
    "MARKET_CAP_RATE_PER_SEC",
    "MARKET_CAP_TOP_N",
    "US_MARKET_DEFAULT_DAYS",
    "collect_index_daily_prices",
    "collect_us_market",
    "refresh_market_caps",
]
