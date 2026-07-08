"""fast_loop 기동 시 + 주기적으로 kis-gateway 에 실시간 체결·호가 구독을 보증.

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

구독 보증 (2026-07-08 추가):
  기동 시 1회 호출은 게이트웨이가 아직 안 떠 있으면 실패하고 재시도가 없었다.
  정전 복구 때 fast_loop 이 gateway 보다 22ms 먼저 떠 초기 구독이 유실됐고,
  실시간 체결·호가가 3거래일 끊겼다. 이를 막기 위해 `run_subscription_maintainer`
  가 기동 직후 1회 + 이후 주기적으로 게이트웨이 상태를 확인해, 구독이 죽었거나
  종목이 빠졌으면 재구독한다. 기동 순서 문제도, 중간에 streamer 가 죽는 경우도
  자동 복구된다. tick stream 이 잠시 비어도 price-scheduler 의 5분 REST 폴링이
  가격 fallback 으로 동작한다 (틱·호가 적재는 fallback 없음 — 그래서 보증이 필요).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import asyncpg
import httpx

logger = logging.getLogger(__name__)

# KIS WebSocket 한 연결당 등록 가능한 건수 상한.
KIS_WS_SUBSCRIPTION_LIMIT = 41

# 종목마다 체결(H0STCNT0)+호가(H0STASP0) 두 채널을 등록하므로 등록 2건/종목.
# 실효 종목 수는 한도의 절반.
_REGISTRATIONS_PER_CODE = 2
MAX_SUBSCRIPTION_CODES = KIS_WS_SUBSCRIPTION_LIMIT // _REGISTRATIONS_PER_CODE

# 구독 보증 루프 기본 주기 (초). 기동 순서로 초기 구독이 실패해도 이 주기 안에
# 복구된다 — 실시간 유실 최대치를 좌우하므로 짧게 둔다.
SUBSCRIPTION_MAINTAIN_INTERVAL_SEC = 60.0

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

    체결+호가 두 채널이라 실효 종목 한도 (20) 를 넘으면 positions 전체 + 최신 시트
    순으로 자른다.
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
        if len(codes) >= MAX_SUBSCRIPTION_CODES:
            logger.warning(
                "sub codes truncated at %d (등록 %d건/종목, KIS 한도 %d) dropped=%d",
                MAX_SUBSCRIPTION_CODES,
                _REGISTRATIONS_PER_CODE,
                KIS_WS_SUBSCRIPTION_LIMIT,
                len(set(position_codes + sheet_codes)) - len(codes),
            )
            break
        codes.append(code)
        seen.add(code)
    return sorted(codes)


async def _post_subscribe(gateway_url: str, codes: list[str], *, timeout: float) -> dict[str, Any]:
    """gateway `/api/realtime/subscribe` 로 구독 요청. 응답 body 반환."""
    async with httpx.AsyncClient() as http:
        resp = await http.post(
            f"{gateway_url}/api/realtime/subscribe",
            json={"codes": codes},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()


async def get_subscription_status(gateway_url: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """gateway `/api/realtime/status` 조회. {is_running, subscription_count, codes}."""
    async with httpx.AsyncClient() as http:
        resp = await http.get(f"{gateway_url}/api/realtime/status", timeout=timeout)
        resp.raise_for_status()
        return resp.json()


async def subscribe_on_startup(
    pool: asyncpg.Pool,
    gateway_url: str,
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """positions + 측정 대기 시트로 gateway subscribe 를 무조건 1회 요청.

    실패 시 예외를 올리지 않고 warning 로그 후 진행. 반환 dict 는 관측/테스트용.
    상태 확인 없이 곧장 POST 하므로, 재구독 보증은 `ensure_subscribed` 를 쓴다.
    """
    codes = await load_subscription_codes(pool)
    if not codes:
        logger.info("gateway subscribe skipped — positions/측정 대기 시트 비어있음")
        return {"codes": [], "skipped": True}

    try:
        body = await _post_subscribe(gateway_url, codes, timeout=timeout)
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


async def ensure_subscribed(
    pool: asyncpg.Pool,
    gateway_url: str,
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """구독이 살아있고 원하는 종목을 다 담고 있는지 확인, 아니면 재구독.

    streamer 가 돌고(is_running) 원하는 종목이 모두 구독돼 있으면 아무것도 하지
    않는다(no-op). 그렇지 않으면(기동 순서로 초기 구독 유실, 게이트웨이 재시작 등)
    재구독한다. status 조회 자체가 실패하면(게이트웨이 미준비) 일단 구독을 시도한다.
    """
    codes = await load_subscription_codes(pool)
    if not codes:
        logger.debug("realtime 구독 보증 skip — 구독 대상 비어있음")
        return {"codes": [], "skipped": True}

    try:
        status = await get_subscription_status(gateway_url, timeout=timeout)
    except Exception as e:
        logger.debug("realtime status 조회 실패, 구독 시도로 진행: %s", e)
        status = None

    if status is not None:
        running = bool(status.get("is_running"))
        missing = sorted(set(codes) - set(status.get("codes", [])))
        if running and not missing:
            return {"codes": codes, "noop": True}
        logger.info(
            "realtime 구독 보증 — running=%s missing=%d → 재구독",
            running,
            len(missing),
        )

    try:
        body = await _post_subscribe(gateway_url, codes, timeout=timeout)
    except Exception as e:
        logger.warning(
            "realtime 재구독 실패 — 다음 주기에 재시도 (codes=%d): %s",
            len(codes),
            e,
        )
        return {"codes": codes, "error": str(e)}

    logger.info(
        "realtime 재구독 OK — codes=%d added=%d total=%d running=%s",
        len(codes),
        len(body.get("added", [])),
        body.get("total_subscriptions", 0),
        body.get("is_running", False),
    )
    return {"codes": codes, "response": body}


async def run_subscription_maintainer(
    pool: asyncpg.Pool,
    gateway_url: str,
    *,
    interval_sec: float = SUBSCRIPTION_MAINTAIN_INTERVAL_SEC,
    stop_event: asyncio.Event | None = None,
) -> None:
    """기동 직후 1회 + 이후 주기적으로 실시간 구독을 보증하는 장기 태스크.

    기동 시 게이트웨이가 아직 안 떠 구독이 실패하거나(컨테이너 기동 순서),
    게이트웨이/streamer 가 나중에 죽어 구독이 비면 다음 주기에 자동 복구한다.
    2026-07-08 정전 복구 때 fast_loop 이 gateway 보다 먼저 떠 초기 구독이
    유실되고 재시도 경로가 없어 실시간 체결·호가가 3거래일 끊긴 사고 대응.
    """
    # 기동 직후 1회는 반드시 보증한다(do-while) — 그래서 첫 구독이 stop 이전에
    # 무조건 걸린다. 이후에는 stop_event 를 기다리며 interval 마다 재점검한다.
    while True:
        try:
            await ensure_subscribed(pool, gateway_url)
        except Exception:
            logger.exception("subscription maintainer iteration 실패")

        if stop_event is None:
            await asyncio.sleep(interval_sec)
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
        except TimeoutError:
            pass
        if stop_event.is_set():
            return


__all__ = [
    "load_subscription_codes",
    "subscribe_on_startup",
    "ensure_subscribed",
    "get_subscription_status",
    "run_subscription_maintainer",
    "KIS_WS_SUBSCRIPTION_LIMIT",
    "MAX_SUBSCRIPTION_CODES",
    "SUBSCRIPTION_MAINTAIN_INTERVAL_SEC",
]
