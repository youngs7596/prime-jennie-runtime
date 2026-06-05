"""5분봉 자동 수집 (시총 상위 30 + 측정 대기 시트) — paper 측정용 수집기.

v2 원본: `/jobs/collect-minute-chart` (services/jobs/app.py:624-699).

v3 어댑터:
- v2 KIS 직접 호출 (rate-limit time.sleep 1/18s) → kis_gateway HTTP
  `/api/market/minute-prices`. asyncio.sleep 으로 ~10 req/s 페이싱 (2026-06-05
  버스트 피크 완화로 v2 의 18 req/s 에서 하향, 아래 _REQ_PER_SEC 주석 참고).
- v2 in-line INSERT 검사 → PostgresPriceRepo.upsert_minute (executemany 일괄).
- 2026-05-08 부터 KIS 분봉 단일 수집자. price_scheduler.collect_minute 은 5종목
  sample placeholder 였던 잔재 잡으로 obsolete (top30 안에 모두 포함되어 100% 중복).

수집 대상 (P2.6, 2026-06-03 교정):
- stock_masters 시총 상위 N (백테스트 참고용 기본 커버리지)
- paper 측정 윈도우가 아직 열려있는 시트의 ticker — v2 잔재 watchlist_histories
  (4-17 동결, v3 writer 없음) 를 대체. 측정하려는 시트의 분봉이 정작 안 쌓이던
  결함을 고침 (P3 1분봉 simulator 의 전제).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from prime_jennie_runtime.kis_gateway.price_repo import PostgresPriceRepo
from prime_jennie_runtime.kis_gateway.schemas import MinutePrice

logger = logging.getLogger(__name__)


# 분봉 수집 페이싱. v2 는 1/18s 였지만 18 req/s 는 KIS 초당 한도(20)에 거의 붙어
# 돌아서, 같은 5분 슬롯의 잔고 폴·시세 호출과 합쳐질 때 합산 피크가 한도를 잠깐
# 스쳤다 (2026-06-05 잔고 throttle 진단의 잔여 증상). 10 req/s 로 낮춰 KIS 한도의
# 절반만 쓰면 나머지 호출에 여유가 생기고, 대상 40여 종목도 4초면 끝나 5분 주기
# 안에서 신선도 손해 없이 버스트 피크가 내려간다.
_REQ_PER_SEC = 10
_MIN_INTERVAL = 1.0 / _REQ_PER_SEC

# 측정 대기 시트 ticker 조회 — paper_outcomes 의 _fetch_pending_sheets 와 같은 기준.
# 30일 안전 상한: 측정이 다시 멈추는 사고가 나도 수집 대상이 무한정 자라
# KIS 호출량이 폭증하는 일을 막는다 (윈도우 최대 10 거래일 ≈ 14 달력일 + 여유).
PENDING_SHEET_TICKERS_SQL = """
    SELECT DISTINCT ps.ticker AS stock_code
    FROM position_sheets ps
    WHERE ps.sheet_id LIKE 'ps_%'
    AND ps.generated_at >= NOW() - INTERVAL '30 days'
    AND NOT EXISTS (
        SELECT 1 FROM paper_outcomes po WHERE po.sheet_id = ps.sheet_id
    )
"""


async def collect_minute_chart(
    pool: Any,
    http: httpx.AsyncClient,
    gateway_url: str,
    *,
    top_n: int = 30,
) -> dict:
    """v2 `/jobs/collect-minute-chart` 포팅 + P2.6 수집 대상 교정.

    1) stock_masters 활성 + 시총 desc 상위 N (default 30)
    2) 측정 윈도우가 열려있는 시트 (paper_outcomes 미적재) 의 ticker 추가
    3) 각 종목 gateway `/api/market/minute-prices` POST → minute_prices upsert
    4) ~18 req/s 페이싱 (v2 와 동일; gateway 전역 limiter 가 최종 방어선)

    Returns: {"target": N, "top_n": N, "pending_sheets_added": N, "upserted": N, "failed": N}
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

        sheet_rows = await conn.fetch(PENDING_SHEET_TICKERS_SQL)
        pending_sheets_added = 0
        for r in sheet_rows:
            if r["stock_code"] not in target_codes:
                target_codes.add(r["stock_code"])
                pending_sheets_added += 1

        target_list = sorted(target_codes)
        logger.info(
            "collect_minute_chart: targets=%d (top%d=%d + pending_sheets=%d)",
            len(target_list),
            top_n,
            top_count,
            pending_sheets_added,
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
        "pending_sheets_added": pending_sheets_added,
        "upserted": upserted,
        "failed": failed,
    }


__all__ = ["collect_minute_chart", "PENDING_SHEET_TICKERS_SQL"]
