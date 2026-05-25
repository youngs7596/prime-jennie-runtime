"""stock_masters 시딩 job.

v2 원본:
- `/jobs/seed-stock-masters` (services/jobs/app.py:2719-2739)  → 얇은 wrapper
- 실제 로직: `prime_jennie/scripts/seed_stock_masters.py:seed_stock_masters`

v3 어댑터:
- 네이버 시총 순위 (`fetch_market_stocks`) + 섹터 매핑 (`build_naver_sector_mapping`)
  은 jobs/crawlers 에 이미 async 로 포팅됨.
- DB 저장은 v2 의 SQLModel UPDATE-or-INSERT 를 asyncpg INSERT ... ON CONFLICT 로 1회 SQL.
- v2 와 동일하게 sector 매핑 미존재 종목은 sector_group="기타" 로 fall through.
- 2026-05-25: security_type 컬럼 채움. kis-gateway search-info 호출해서
  ST→STOCK / EF→ETF / EN→ETN / EW→ELW 매핑. universe SQL 이 STOCK 만 사용.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .crawlers.naver import build_naver_sector_mapping
from .crawlers.naver_market import fetch_market_stocks
from .sector_taxonomy import get_sector_group

logger = logging.getLogger(__name__)


SUPPORTED_MARKETS = ("KOSPI", "KOSDAQ")
DEFAULT_SECTOR_GROUP = "기타"
DEFAULT_SECURITY_TYPE = "STOCK"
PROGRESS_EVERY = 100
SECURITY_TYPE_RATE_LIMIT_DELAY_SEC = 0.06  # KIS 초당 ~20 호출 안전 마진

_SCTY_GRP_TO_TYPE = {
    "ST": "STOCK",  # 주권 (보통주/우선주)
    "EF": "ETF",
    "EN": "ETN",
    "EW": "ELW",
}


async def _fetch_security_type(
    http: httpx.AsyncClient, kis_gateway_url: str, stock_code: str
) -> str:
    """KIS search-info 응답의 scty_grp_id_cd → security_type 매핑.

    실패 시 'STOCK' default (안전 측 — universe SQL 이 STOCK 만 보므로
    분류 실패 종목은 포함됨).
    """
    url = f"{kis_gateway_url.rstrip('/')}/api/quotations/search-info/{stock_code}"
    try:
        resp = await http.get(url, timeout=10.0)
        if resp.status_code == 404:
            return DEFAULT_SECURITY_TYPE
        resp.raise_for_status()
        info = resp.json() or {}
    except Exception:
        logger.warning("search-info HTTP 실패 %s — STOCK fallback", stock_code, exc_info=True)
        return DEFAULT_SECURITY_TYPE
    grp = info.get("scty_grp_id_cd") or ""
    return _SCTY_GRP_TO_TYPE.get(grp, DEFAULT_SECURITY_TYPE)


async def seed_stock_masters(
    pool: Any,
    http: httpx.AsyncClient,
    *,
    market: str = "KOSPI",
    kis_gateway_url: str | None = None,
) -> dict[str, int]:
    """v2 `seed_stock_masters` 포팅 + security_type 분류.

    네이버 시총 순위 → stock_masters UPSERT. 신규는 INSERT, 존재하면 stock_name /
    market_cap (값 있을 때만) / sector_naver+sector_group (sector 있을 때만) /
    security_type / is_active=True 로 갱신. updated_at 은 NOW().

    Args:
        kis_gateway_url: 주어지면 종목마다 KIS search-info 호출해서 ST/EF/EN/EW
            매핑으로 security_type 채움. None 이면 'STOCK' default (기존 동작).

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
                if kis_gateway_url:
                    security_type = await _fetch_security_type(http, kis_gateway_url, s.stock_code)
                    await asyncio.sleep(SECURITY_TYPE_RATE_LIMIT_DELAY_SEC)
                else:
                    security_type = DEFAULT_SECURITY_TYPE

                # COALESCE 패턴으로 v2 의 "값 있을 때만 덮어쓰기" 의미 보존.
                await conn.execute(
                    "INSERT INTO stock_masters "
                    "(stock_code, stock_name, market, market_cap, sector_naver, "
                    "sector_group, security_type, is_active, updated_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE, NOW()) "
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
                    "security_type = EXCLUDED.security_type, "
                    "is_active = TRUE, "
                    "updated_at = NOW()",
                    s.stock_code,
                    s.stock_name,
                    market,
                    s.market_cap if s.market_cap else None,
                    sector or None,
                    group,
                    security_type,
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
