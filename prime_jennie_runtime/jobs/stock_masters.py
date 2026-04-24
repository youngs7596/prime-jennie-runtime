"""stock_masters 시딩 job.

v2 원본:
- `/jobs/seed-stock-masters` (services/jobs/app.py:2719-2739)  → 얇은 wrapper
- 실제 로직: `prime_jennie/scripts/seed_stock_masters.py:seed_stock_masters`

v3 어댑터:
- 네이버 시총 순위 (`fetch_market_stocks`) + 섹터 매핑 (`build_naver_sector_mapping`)
  은 jobs/crawlers 에 이미 async 로 포팅됨.
- DB 저장은 v2 의 SQLModel UPDATE-or-INSERT 를 asyncpg INSERT ... ON CONFLICT 로 1회 SQL.
- v2 와 동일하게 sector 매핑 미존재 종목은 sector_group="기타" 로 fall through.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .crawlers.naver import build_naver_sector_mapping
from .crawlers.naver_market import fetch_market_stocks
from .sector_taxonomy import get_sector_group

logger = logging.getLogger(__name__)


SUPPORTED_MARKETS = ("KOSPI", "KOSDAQ")
DEFAULT_SECTOR_GROUP = "기타"
PROGRESS_EVERY = 100


async def seed_stock_masters(
    pool: Any,
    http: httpx.AsyncClient,
    *,
    market: str = "KOSPI",
) -> dict[str, int]:
    """v2 `seed_stock_masters` 포팅.

    네이버 시총 순위 → stock_masters UPSERT. 신규는 INSERT, 존재하면 stock_name /
    market_cap (값 있을 때만) / sector_naver+sector_group (sector 있을 때만) /
    is_active=True 로 갱신. updated_at 은 NOW().

    Returns: {"inserted": N, "updated": N, "total": N, "failed": N}.
    """
    if market not in SUPPORTED_MARKETS:
        raise ValueError(f"market 은 {SUPPORTED_MARKETS} 중 하나여야 함: {market!r}")

    logger.info("seed_stock_masters: fetching %s ...", market)
    market_stocks = await fetch_market_stocks(http, market=market)
    logger.info("seed_stock_masters: fetched=%d", len(market_stocks))
    if not market_stocks:
        return {"inserted": 0, "updated": 0, "total": 0, "failed": 0}

    logger.info("seed_stock_masters: building naver sector mapping ...")
    naver_sectors = await build_naver_sector_mapping(http)
    logger.info("seed_stock_masters: sector mapping=%d", len(naver_sectors))

    inserted = 0
    updated = 0
    failed = 0

    async with pool.acquire() as conn:
        existing_codes = {
            r["stock_code"] for r in await conn.fetch("SELECT stock_code FROM stock_masters")
        }
        logger.info("seed_stock_masters: existing in DB=%d", len(existing_codes))

        for i, s in enumerate(market_stocks, 1):
            try:
                sector = naver_sectors.get(s.stock_code, "")
                group = (
                    get_sector_group(sector, stock_code=s.stock_code).value
                    if sector
                    else DEFAULT_SECTOR_GROUP
                )

                # COALESCE 패턴으로 v2 의 "값 있을 때만 덮어쓰기" 의미 보존.
                await conn.execute(
                    "INSERT INTO stock_masters "
                    "(stock_code, stock_name, market, market_cap, sector_naver, "
                    "sector_group, is_active, updated_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, TRUE, NOW()) "
                    "ON CONFLICT (stock_code) DO UPDATE SET "
                    "stock_name = EXCLUDED.stock_name, "
                    "market_cap = COALESCE("
                    "    EXCLUDED.market_cap, stock_masters.market_cap"
                    "), "
                    "sector_naver = COALESCE("
                    "    NULLIF(EXCLUDED.sector_naver, ''), stock_masters.sector_naver"
                    "), "
                    "sector_group = CASE "
                    "    WHEN EXCLUDED.sector_naver IS NULL "
                    "      OR EXCLUDED.sector_naver = '' "
                    "    THEN stock_masters.sector_group "
                    "    ELSE EXCLUDED.sector_group "
                    "END, "
                    "is_active = TRUE, "
                    "updated_at = NOW()",
                    s.stock_code,
                    s.stock_name,
                    market,
                    s.market_cap if s.market_cap else None,
                    sector or None,
                    group,
                )

                if s.stock_code in existing_codes:
                    updated += 1
                else:
                    inserted += 1
                    existing_codes.add(s.stock_code)

            except Exception as e:
                failed += 1
                logger.warning("seed failed %s: %s", s.stock_code, e)

            if i % PROGRESS_EVERY == 0:
                logger.info("seed progress: %d/%d", i, len(market_stocks))

    total = inserted + updated
    logger.info(
        "seed_stock_masters: inserted=%d updated=%d total=%d failed=%d",
        inserted,
        updated,
        total,
        failed,
    )
    return {"inserted": inserted, "updated": updated, "total": total, "failed": failed}


__all__ = [
    "DEFAULT_SECTOR_GROUP",
    "SUPPORTED_MARKETS",
    "seed_stock_masters",
]
