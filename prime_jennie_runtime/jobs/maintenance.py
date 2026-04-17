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

import httpx

from .crawlers.fnguide import crawl_fnguide_consensus
from .crawlers.naver import (
    build_naver_sector_mapping,
    crawl_naver_fundamentals,
    crawl_naver_roe,
)
from .crawlers.naver_market import fetch_investor_flows
from .sector_taxonomy import get_sector_group

logger = logging.getLogger(__name__)


DEFAULT_CLEANUP_DAYS = 365

# v2 `/jobs/contract-smoke-test` 임계값/범위 — 값을 다시 튜닝하지 말 것.
CONTRACT_SMOKE_SENTINEL = "005930"  # 삼성전자
_PER_RANGE = (1.0, 200.0)
_PBR_RANGE = (0.1, 50.0)
_ROE_RANGE = (-50.0, 100.0)
_SECTOR_MIN = 500
_INVESTOR_FLOW_TOLERANCE_EOK = 10000.0
_ROE_CROSS_CHECK_DIFF_PP = 10.0


class ContractSmokeError(RuntimeError):
    """크롤러 contract 가 깨졌을 때 raise — scheduler runner 가 실패로 기록."""


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


async def update_naver_sectors(
    pool: Any,
    http: httpx.AsyncClient,
) -> None:
    """v2 `/jobs/update-naver-sectors` 포팅 (app.py:2353-2373).

    네이버 업종 분류 mapping 을 크롤링해 `stock_masters.sector_naver`/`sector_group`
    컬럼을 갱신. v2 는 SQLModel row 단위로 update 하지만 asyncpg 로 포팅하면서
    단일 UPDATE 쿼리로 묶었다 (stock_code → sector 매핑만 보내면 되므로 로직
    동일). `sector_group` 은 `get_sector_group` 으로 계산 (override 우선).

    v2 스케줄: `0 20 * * 0` (일요일 20:00). timeout 15분.
    """
    mapping = await build_naver_sector_mapping(http)
    if not mapping:
        logger.warning("update_naver_sectors: empty mapping — skip")
        return

    updated = 0
    async with pool.acquire() as conn, conn.transaction():
        for code, sector in mapping.items():
            sector_group = get_sector_group(sector, stock_code=code).value
            result = await conn.execute(
                "UPDATE stock_masters SET sector_naver=$1, sector_group=$2, "
                "updated_at=NOW() WHERE stock_code=$3",
                sector,
                sector_group,
                code,
            )
            if isinstance(result, str) and result.startswith("UPDATE "):
                try:
                    updated += int(result.split(" ", 1)[1])
                except (IndexError, ValueError):
                    pass
    logger.info(
        "update_naver_sectors: mapping=%d, updated=%d rows",
        len(mapping),
        updated,
    )


async def contract_smoke_test(
    http: httpx.AsyncClient,
    news_crawler: Any,
    *,
    sentinel: str = CONTRACT_SMOKE_SENTINEL,
    sentinel_name: str = "삼성전자",
) -> None:
    """v2 `/jobs/contract-smoke-test` 포팅.

    6 개 외부 크롤러를 sentinel 종목(삼성전자)으로 호출해 응답 구조/값 범위를
    검증. 하나라도 실패하면 `ContractSmokeError`. 검증 규칙은 v2 (app.py
    2745-2883) 원문 그대로:

    - fundamentals: PER ∈ (1, 200), PBR ∈ (0.1, 50)
    - roe: ∈ (-50, 100)
    - news: 최소 1건 + 비어있지 않은 headline
    - sector mapping: 최소 500종목 + sentinel 포함
    - fnguide consensus: forward_per ∈ (1, 200)
    - investor flows: 외인+기관+개인 합계 |x| ≤ 10,000 억
    - roe cross-check: fundamentals.roe 와 crawl_naver_roe 결과 차이 ≤ 10pp

    `news_crawler` 는 Track E `NaverNewsCrawler` (duck-typed: `crawl(list[str])`).
    DB 접근 없음 — 실행 리스크가 가장 낮아 운영 모니터링 용.
    """
    passed: list[str] = []
    failed: list[str] = []

    try:
        fund = await crawl_naver_fundamentals(http, sentinel)
        if fund is None:
            failed.append("fundamentals: returned None")
        elif fund.per is None or not (_PER_RANGE[0] < fund.per < _PER_RANGE[1]):
            failed.append(f"fundamentals: PER out of range ({fund.per})")
        elif fund.pbr is None or not (_PBR_RANGE[0] < fund.pbr < _PBR_RANGE[1]):
            failed.append(f"fundamentals: PBR out of range ({fund.pbr})")
        else:
            passed.append(
                f"fundamentals: PER={fund.per}, PBR={fund.pbr}, "
                f"ROE={fund.roe}, Q={fund.quarter_name}"
            )
    except Exception as e:
        fund = None
        failed.append(f"fundamentals: exception — {e}")

    try:
        roe = await crawl_naver_roe(http, sentinel)
        if roe is None:
            failed.append("roe: returned None")
        elif not (_ROE_RANGE[0] < roe < _ROE_RANGE[1]):
            failed.append(f"roe: out of range ({roe})")
        else:
            passed.append(f"roe: {roe}")
    except Exception as e:
        roe = None
        failed.append(f"roe: exception — {e}")

    try:
        articles = await news_crawler.crawl([sentinel])
        if not articles:
            failed.append("news: no articles returned")
        elif not getattr(articles[0], "title", None) and not getattr(
            articles[0], "headline", None
        ):
            failed.append("news: empty headline")
        else:
            passed.append(f"news: {len(articles)} articles")
        _ = sentinel_name  # v2 와 시그니처 호환 — news crawler 는 name 을 보지 않음
    except Exception as e:
        failed.append(f"news: exception — {e}")

    try:
        mapping = await build_naver_sector_mapping(http)
        if len(mapping) < _SECTOR_MIN:
            failed.append(f"sector_mapping: too few stocks ({len(mapping)})")
        elif sentinel not in mapping:
            failed.append("sector_mapping: sentinel not found")
        else:
            passed.append(f"sector_mapping: {len(mapping)} stocks")
    except Exception as e:
        failed.append(f"sector_mapping: exception — {e}")

    try:
        consensus = await crawl_fnguide_consensus(http, sentinel)
        if consensus is None:
            failed.append("fnguide_consensus: returned None")
        elif consensus.forward_per is None or not (
            _PER_RANGE[0] < consensus.forward_per < _PER_RANGE[1]
        ):
            failed.append(
                f"fnguide_consensus: forward_per out of range ({consensus.forward_per})"
            )
        else:
            passed.append(
                f"fnguide_consensus: fwd_PER={consensus.forward_per}, "
                f"fwd_EPS={consensus.forward_eps}"
            )
    except Exception as e:
        failed.append(f"fnguide_consensus: exception — {e}")

    try:
        test_flows = None
        for d in range(7):
            test_date = date.today() - timedelta(days=d)
            bizdate = test_date.strftime("%Y%m%d")
            test_flows = await fetch_investor_flows(http, "kospi", bizdate)
            if test_flows is not None:
                break
        if test_flows is None:
            failed.append("investor_flows: no data in last 7 days")
        else:
            total = (
                test_flows.foreign_net
                + test_flows.institutional_net
                + test_flows.retail_net
            )
            if abs(total) > _INVESTOR_FLOW_TOLERANCE_EOK:
                failed.append(
                    f"investor_flows: cross-check failed "
                    f"(foreign={test_flows.foreign_net}, "
                    f"inst={test_flows.institutional_net}, "
                    f"retail={test_flows.retail_net}, total={total})"
                )
            else:
                passed.append(
                    f"investor_flows: foreign={test_flows.foreign_net}, "
                    f"inst={test_flows.institutional_net}, "
                    f"retail={test_flows.retail_net}"
                )
    except Exception as e:
        failed.append(f"investor_flows: exception — {e}")

    try:
        if fund is not None and fund.roe is not None and roe is not None:
            roe_diff = abs(fund.roe - roe)
            if roe_diff > _ROE_CROSS_CHECK_DIFF_PP:
                failed.append(
                    f"roe_cross_check: fundamentals ROE={fund.roe} vs "
                    f"crawl_roe={roe}, diff={roe_diff:.1f}pp"
                )
            else:
                passed.append(f"roe_cross_check: diff={roe_diff:.1f}pp (OK)")
    except Exception as e:
        failed.append(f"roe_cross_check: exception — {e}")

    logger.info("contract_smoke_test: %d passed, %d failed", len(passed), len(failed))
    if failed:
        detail = "\n".join([f"  ✗ {f}" for f in failed])
        logger.warning("contract_smoke_test failures:\n%s", detail)
        raise ContractSmokeError(
            f"{len(failed)} contract(s) broken: " + "; ".join(failed)
        )


__all__ = [
    "CONTRACT_SMOKE_SENTINEL",
    "DEFAULT_CLEANUP_DAYS",
    "ContractSmokeError",
    "cleanup_old_data",
    "contract_smoke_test",
    "update_naver_sectors",
]
