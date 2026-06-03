"""fast_loop 기동 시 kis-gateway 에 실시간 체결가 구독 요청 발행.

v2 원본:
  - prime_jennie/services/monitor/app.py:_subscribe_to_gateway
  - prime_jennie/services/scanner/app.py:_subscribe_to_gateway

v2 에서는 monitor + scanner 가 각자 gateway `/api/realtime/subscribe` 를 호출해
KIS WebSocket 구독을 trigger 했다. v3 에서 이 두 서비스는 fast_loop 로 통합됐지만
subscribe 호출 경로가 포팅되지 않아 gateway streamer 가 dead path 상태였다.
이 모듈이 그 갭을 메운다.

대상 종목 (P2.6, 2026-06-03 교정):
  - positions 전체 (현재 보유) — 우선 순위 최상
  - paper 측정 윈도우가 열려있는 시트의 ticker — v2 잔재 watchlist_histories
    (4-17 동결, v3 writer 없음) 를 대체
  - KIS WebSocket 등록 한도 (41) 안에서 자른다. positions 먼저, 시트는 최신순.

호출 실패는 치명적이지 않다 (warning 만 남기고 fast_loop 은 계속 구동).
실패 시 tick_loop 는 stream 에서 아무것도 받지 못하지만 polling fallback
(price-scheduler 의 5분 REST) 이 별도로 동작하고 있으므로 안전하게 degrade.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg
import httpx

logger = logging.getLogger(__name__)

# KIS WebSocket 한 연결당 등록 가능한 종목 수 상한.
KIS_WS_SUBSCRIPTION_LIMIT = 41

# 측정 윈도우가 열려있는 시트의 ticker — jobs/minute_chart.py 와 같은 기준.
# 최신 시트 우선으로 정렬해 한도 초과 시 오래된 시트부터 떨어져 나가게 한다.
_PENDING_SHEET_TICKERS_SQL = """
    SELECT ps.ticker AS stock_code, MAX(ps.generated_at) AS latest_generated_at
    FROM position_sheets ps
    WHERE ps.sheet_id LIKE 'ps_%'
    AND ps.generated_at >= NOW() - INTERVAL '30 days'
    AND NOT EXISTS (
        SELECT 1 FROM paper_outcomes po WHERE po.sheet_id = ps.sheet_id
    )
    GROUP BY ps.ticker
    ORDER BY latest_generated_at DESC
"""


async def load_subscription_codes(pool: asyncpg.Pool) -> list[str]:
    """positions + 측정 대기 시트 ticker 에서 구독 대상 종목 코드를 수집.

    KIS WebSocket 등록 한도 (41) 를 넘으면 positions 전체 + 최신 시트 순으로 자른다.
    """
    async with pool.acquire() as conn:
        pos_rows = await conn.fetch("SELECT stock_code FROM positions")
        position_codes = [r["stock_code"] for r in pos_rows]

        sheet_rows = await conn.fetch(_PENDING_SHEET_TICKERS_SQL)
        sheet_codes = [r["stock_code"] for r in sheet_rows]

    # positions 우선 + 시트는 최신순으로 한도까지 채움.
    codes: list[str] = []
    seen: set[str] = set()
    for code in position_codes + sheet_codes:
        if code in seen:
            continue
        if len(codes) >= KIS_WS_SUBSCRIPTION_LIMIT:
            logger.warning(
                "subscription codes truncated at KIS WS limit (%d) — dropped=%d",
                KIS_WS_SUBSCRIPTION_LIMIT,
                len(set(position_codes + sheet_codes)) - len(codes),
            )
            break
        codes.append(code)
        seen.add(code)
    return sorted(codes)


async def subscribe_on_startup(
    pool: asyncpg.Pool,
    gateway_url: str,
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """fast_loop 기동 시 1회 호출. positions + 측정 대기 시트로 gateway subscribe.

    실패 시 예외를 올리지 않고 warning 로그 후 진행. 반환 dict 는 관측/테스트용.
    """
    codes = await load_subscription_codes(pool)
    if not codes:
        logger.info("gateway subscribe skipped — positions/측정 대기 시트 비어있음")
        return {"codes": [], "skipped": True}

    try:
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                f"{gateway_url}/api/realtime/subscribe",
                json={"codes": codes},
                timeout=timeout,
            )
            resp.raise_for_status()
            body = resp.json()
    except Exception as e:
        logger.warning(
            "gateway subscribe 실패 — tick stream 비활성 상태로 진행 (codes=%d): %s",
            len(codes),
            e,
        )
        return {"codes": codes, "error": str(e)}

    logger.info(
        "gateway subscribe OK — codes=%d added=%d total=%d running=%s",
        len(codes),
        len(body.get("added", [])),
        body.get("total_subscriptions", 0),
        body.get("is_running", False),
    )
    return {"codes": codes, "response": body}


__all__ = ["load_subscription_codes", "subscribe_on_startup", "KIS_WS_SUBSCRIPTION_LIMIT"]
