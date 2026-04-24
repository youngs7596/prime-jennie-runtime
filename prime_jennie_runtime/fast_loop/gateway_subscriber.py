"""fast_loop 기동 시 kis-gateway 에 실시간 체결가 구독 요청 발행.

v2 원본:
  - prime_jennie/services/monitor/app.py:_subscribe_to_gateway
  - prime_jennie/services/scanner/app.py:_subscribe_to_gateway

v2 에서는 monitor + scanner 가 각자 gateway `/api/realtime/subscribe` 를 호출해
KIS WebSocket 구독을 trigger 했다. v3 에서 이 두 서비스는 fast_loop 로 통합됐지만
subscribe 호출 경로가 포팅되지 않아 gateway streamer 가 dead path 상태였다.
이 모듈이 그 갭을 메운다.

대상 종목:
  - positions 전체 (현재 보유)
  - watchlist_histories 최신 snapshot_date, is_active = TRUE

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


async def load_subscription_codes(pool: asyncpg.Pool) -> list[str]:
    """positions + 최신 watchlist_histories 에서 구독 대상 종목 코드를 수집."""
    codes: set[str] = set()
    async with pool.acquire() as conn:
        pos_rows = await conn.fetch("SELECT stock_code FROM positions")
        codes.update(r["stock_code"] for r in pos_rows)

        latest_date = await conn.fetchval(
            "SELECT snapshot_date FROM watchlist_histories ORDER BY snapshot_date DESC LIMIT 1"
        )
        if latest_date is not None:
            wl_rows = await conn.fetch(
                "SELECT stock_code FROM watchlist_histories "
                "WHERE snapshot_date = $1 AND is_active = TRUE",
                latest_date,
            )
            codes.update(r["stock_code"] for r in wl_rows)
    return sorted(codes)


async def subscribe_on_startup(
    pool: asyncpg.Pool,
    gateway_url: str,
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """fast_loop 기동 시 1회 호출. positions + watchlist 로 gateway subscribe.

    실패 시 예외를 올리지 않고 warning 로그 후 진행. 반환 dict 는 관측/테스트용.
    """
    codes = await load_subscription_codes(pool)
    if not codes:
        logger.info("gateway subscribe skipped — positions/watchlist 비어있음")
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


__all__ = ["load_subscription_codes", "subscribe_on_startup"]
