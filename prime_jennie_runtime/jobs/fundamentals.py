"""기업 펀더멘털/컨센서스 수집 jobs.

v2 원본:
- `/jobs/collect-consensus` (app.py:2508-2582) — 주간, FnGuide → Naver fallback
- `/jobs/collect-naver-roe` (app.py:2588-2639) — 월간, ROE 만
- `/jobs/collect-quarterly-financials` (app.py:2642-2713) — 분기, PER/PBR/ROE

세 job 모두 시총 상위 300종목 순회 + asyncio.sleep throttle. 크롤러 (fnguide/naver) 는
별 모듈에 이미 async 로 포팅되어 있다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any

import httpx

from .crawlers.fnguide import crawl_consensus
from .crawlers.naver import crawl_naver_fundamentals, crawl_naver_roe

logger = logging.getLogger(__name__)


# v2 원문 상수 (재튜닝 금지).
FUND_TOP_N = 300
CONSENSUS_THROTTLE_SEC = 0.5
ROE_THROTTLE_SEC = 0.3
QUARTERLY_THROTTLE_SEC = 0.5
PROGRESS_EVERY = 100


async def _top_active_stocks(pool: Any, top_n: int) -> list[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT stock_code FROM stock_masters WHERE is_active = TRUE "
            "ORDER BY market_cap DESC NULLS LAST LIMIT $1",
            top_n,
        )
    return [r["stock_code"] for r in rows]


async def collect_consensus(
    pool: Any,
    http: httpx.AsyncClient,
    *,
    top_n: int = FUND_TOP_N,
    throttle_sec: float = CONSENSUS_THROTTLE_SEC,
) -> None:
    """v2 `/jobs/collect-consensus` 포팅.

    FnGuide 우선 → 실패 시 Naver fallback (`crawl_consensus` 가 두 소스 묶음).
    UPSERT 대상은 stock_consensus (PK: stock_code, trade_date=오늘).
    """
    codes = await _top_active_stocks(pool, top_n)
    logger.info("collect_consensus: candidates=%d", len(codes))
    today = date.today()

    updated = 0
    fnguide_ok = 0
    naver_ok = 0
    failed = 0
    fnguide_fingerprints: set[tuple[Any, ...]] = set()

    async with pool.acquire() as conn:
        for idx, code in enumerate(codes, 1):
            try:
                data = await crawl_consensus(http, code)
            except Exception as e:
                failed += 1
                logger.warning("consensus crawl failed %s: %s", code, e)
                data = None

            if data is None:
                failed += 1
            else:
                await conn.execute(
                    "INSERT INTO stock_consensus "
                    "(stock_code, trade_date, forward_per, forward_eps, forward_roe, "
                    "target_price, analyst_count, investment_opinion, source) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
                    "ON CONFLICT (stock_code, trade_date) DO UPDATE SET "
                    "forward_per=EXCLUDED.forward_per, "
                    "forward_eps=EXCLUDED.forward_eps, "
                    "forward_roe=EXCLUDED.forward_roe, "
                    "target_price=EXCLUDED.target_price, "
                    "analyst_count=EXCLUDED.analyst_count, "
                    "investment_opinion=EXCLUDED.investment_opinion, "
                    "source=EXCLUDED.source",
                    code,
                    today,
                    data.forward_per,
                    data.forward_eps,
                    data.forward_roe,
                    data.target_price,
                    data.analyst_count,
                    data.investment_opinion,
                    data.source,
                )
                updated += 1
                if data.source == "FNGUIDE":
                    fnguide_ok += 1
                    fnguide_fingerprints.add(
                        (data.target_price, data.forward_eps, data.forward_per)
                    )
                else:
                    naver_ok += 1

            if idx % PROGRESS_EVERY == 0:
                logger.info("consensus progress: %d/%d updated=%d", idx, len(codes), updated)
            await asyncio.sleep(throttle_sec)

    logger.info(
        "collect_consensus: updated=%d fnguide=%d naver=%d failed=%d of %d distinct=%d",
        updated,
        fnguide_ok,
        naver_ok,
        failed,
        len(codes),
        len(fnguide_fingerprints),
    )
    _assert_not_degenerate(fnguide_ok, len(fnguide_fingerprints))


# 서로 다른 종목이 이만큼 모였는데 값이 한 가지뿐이면 정상일 수 없다.
DEGENERATE_MIN_SAMPLE = 20


def _assert_not_degenerate(fnguide_rows: int, distinct_values: int) -> None:
    """전 종목이 같은 값이면 잡을 실패로 떨어뜨린다.

    2026-07-02~07-27 에 213 종목이 전부 삼성전자 숫자를 받아 적고도 8거래일 동안
    성공으로 기록됐다. 크롤러가 페이지 종목을 대조하게 됐으니 그 경로는 막혔지만,
    같은 부류의 사고를 값의 모양만으로 한 번 더 거른다. 여기서 예외를 던지면
    scheduled_job_runs 에 failed 로 남아 사람 눈에 걸린다.
    """
    if fnguide_rows >= DEGENERATE_MIN_SAMPLE and distinct_values <= 1:
        raise RuntimeError(
            f"consensus degenerate: {fnguide_rows}종목이 전부 같은 값 "
            f"(distinct={distinct_values}) — 출처가 종목을 구분하지 못하고 있다"
        )


async def collect_naver_roe(
    pool: Any,
    http: httpx.AsyncClient,
    *,
    top_n: int = FUND_TOP_N,
    throttle_sec: float = ROE_THROTTLE_SEC,
) -> None:
    """v2 `/jobs/collect-naver-roe` 포팅 — ROE 만 갱신 (UPSERT, 다른 컬럼 보존)."""
    codes = await _top_active_stocks(pool, top_n)
    logger.info("collect_naver_roe: candidates=%d", len(codes))
    today = date.today()

    updated = 0
    errors = 0

    async with pool.acquire() as conn:
        for idx, code in enumerate(codes, 1):
            try:
                roe = await crawl_naver_roe(http, code)
            except Exception as e:
                errors += 1
                logger.warning("naver roe crawl failed %s: %s", code, e)
                roe = None

            if roe is None:
                errors += 1
            else:
                # PER/PBR 값을 덮어쓰지 않기 위해 UPDATE 우선 → 미존재면 INSERT.
                # asyncpg 단일 SQL: ON CONFLICT 시 roe 만 EXCLUDED 로 갱신.
                await conn.execute(
                    "INSERT INTO stock_fundamentals (stock_code, trade_date, roe) "
                    "VALUES ($1, $2, $3) "
                    "ON CONFLICT (stock_code, trade_date) DO UPDATE SET "
                    "roe=EXCLUDED.roe, updated_at=NOW()",
                    code,
                    today,
                    roe,
                )
                updated += 1

            if idx % PROGRESS_EVERY == 0:
                logger.info("naver_roe progress: %d/%d updated=%d", idx, len(codes), updated)
            await asyncio.sleep(throttle_sec)

    logger.info("collect_naver_roe: updated=%d errors=%d of %d", updated, errors, len(codes))


async def collect_quarterly_financials(
    pool: Any,
    http: httpx.AsyncClient,
    *,
    top_n: int = FUND_TOP_N,
    throttle_sec: float = QUARTERLY_THROTTLE_SEC,
) -> None:
    """v2 `/jobs/collect-quarterly-financials` 포팅 — PER/PBR/ROE UPSERT.

    v2 는 `if result.per is not None: existing.per = result.per` 식으로 None 컬럼은
    보존했다. asyncpg 의 단일 INSERT ... ON CONFLICT 로 동등 의미를 유지하기 위해
    EXCLUDED 가 NULL 이면 기존값 보존하는 COALESCE 패턴 사용.

    **주기는 2026-08-22 에 분기(1·4·7·10월 15일)에서 매 거래일 장마감 뒤로 바뀌었다.**
    이름은 v2 잡 이름을 그대로 물려받은 것이고 하는 일은 네이버 종목 페이지에서
    PER·PBR·ROE 를 긁는 것뿐이다. PER·PBR 은 현재 주가로 계산되는 값이라 분기마다
    받으면 그 사이 주가가 오른 만큼 통째로 어긋난다 — 실제로 8-22 점검에서 가치
    점수(20점, 최대 비중)가 7-15 주가 기준 배수로 매겨지고 있었다.
    """
    codes = await _top_active_stocks(pool, top_n)
    logger.info("collect_quarterly_financials: candidates=%d", len(codes))
    today = date.today()

    updated = 0
    errors = 0

    async with pool.acquire() as conn:
        for idx, code in enumerate(codes, 1):
            try:
                result = await crawl_naver_fundamentals(http, code)
            except Exception as e:
                errors += 1
                logger.warning("naver fundamentals crawl failed %s: %s", code, e)
                result = None

            if result is None:
                errors += 1
            else:
                await conn.execute(
                    "INSERT INTO stock_fundamentals "
                    "(stock_code, trade_date, per, pbr, roe) "
                    "VALUES ($1, $2, $3, $4, $5) "
                    "ON CONFLICT (stock_code, trade_date) DO UPDATE SET "
                    "per=COALESCE(EXCLUDED.per, stock_fundamentals.per), "
                    "pbr=COALESCE(EXCLUDED.pbr, stock_fundamentals.pbr), "
                    "roe=COALESCE(EXCLUDED.roe, stock_fundamentals.roe), "
                    "updated_at=NOW()",
                    code,
                    today,
                    result.per,
                    result.pbr,
                    result.roe,
                )
                updated += 1

            if idx % PROGRESS_EVERY == 0:
                logger.info("quarterly progress: %d/%d updated=%d", idx, len(codes), updated)
            await asyncio.sleep(throttle_sec)

    logger.info(
        "collect_quarterly_financials: updated=%d errors=%d of %d",
        updated,
        errors,
        len(codes),
    )


__all__ = [
    "CONSENSUS_THROTTLE_SEC",
    "FUND_TOP_N",
    "PROGRESS_EVERY",
    "QUARTERLY_THROTTLE_SEC",
    "ROE_THROTTLE_SEC",
    "collect_consensus",
    "collect_naver_roe",
    "collect_quarterly_financials",
]
