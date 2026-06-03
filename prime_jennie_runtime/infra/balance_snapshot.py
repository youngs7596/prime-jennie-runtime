"""잔고 스냅샷 — 단일 발행 + 다중 구독 (시세 tick stream 과 같은 철학).

KIS 원장은 같은 계좌에 대한 근접/동시 잔고 조회를 EGW00201 로 거부한다 (2026-06-03
실측). 조회가 필요한 서비스들이 각자 gateway 를 거쳐 KIS 를 부르면 서로 충돌하므로,
잔고도 시세처럼 "한 곳이 수집해서 발행하고, 나머지는 구독" 으로 만든다.

- 발행자 (유일): monitor 의 LivePositionsPoller — 30초(장중)/300초(장외) 주기로
  gateway 를 호출해 ``monitoring:live_positions`` (v2 호환 키) 에 스냅샷을 쓴다.
- 구독자: 조회·표시·컨텍스트 용도의 모든 소비자 (dashboard / telegram / briefing /
  asset snapshot). 이 모듈의 reader 를 쓴다.
- 의도적으로 구독하지 않는 곳: ground truth 검증 (positions_reconcile / sync_positions)
  과 매매 직전 sizing (fast_loop) — 이들은 gateway HTTP 를 직접 부른다 (gateway 의
  잔고 캐시 + EGW00201 재시도가 보호).

스냅샷 형식 (monitor/poller.py 가 발행):
    {"positions": [...], "updated_at": iso8601,
     "cash_balance": int, "total_asset": int, "stock_eval_amount": int}
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# v2 호환 키 — monitor 의 LivePositionsPoller 가 발행. 이 모듈이 키의 단일 정의처.
LIVE_POSITIONS_KEY = "monitoring:live_positions"

# 스냅샷 최대 허용 나이 (기본). monitor 의 장외 polling 주기 (300s) + 여유.
# 이보다 오래됐으면 monitor 가 죽었다는 신호 → HTTP fallback 이 맞다.
DEFAULT_MAX_AGE_SEC = 600.0


async def read_balance_snapshot(
    redis_client: Any,
    *,
    max_age_sec: float = DEFAULT_MAX_AGE_SEC,
) -> dict[str, Any] | None:
    """monitor 가 발행한 잔고 스냅샷을 읽는다. 없거나 너무 오래되면 None.

    Redis 오류·파싱 오류는 모두 None (fail-open) — 호출자가 HTTP fallback 으로 우회.
    """
    try:
        raw = await redis_client.get(LIVE_POSITIONS_KEY)
    except Exception:
        logger.debug("balance snapshot redis read failed", exc_info=True)
        return None
    if not raw:
        return None

    try:
        snap = json.loads(raw)
        updated_at = datetime.fromisoformat(snap["updated_at"])
    except Exception:
        logger.debug("balance snapshot parse failed", exc_info=True)
        return None

    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - updated_at).total_seconds()
    if age > max_age_sec:
        logger.debug("balance snapshot too old: %.0fs > %.0fs", age, max_age_sec)
        return None
    return snap


async def get_balance_snapshot_or_fetch(
    redis_client: Any,
    gateway_url: str,
    *,
    http: httpx.AsyncClient | None = None,
    max_age_sec: float = DEFAULT_MAX_AGE_SEC,
    timeout: float = 5.0,
) -> dict[str, Any] | None:
    """스냅샷 우선, 없으면 gateway HTTP fallback.

    fallback 경로는 gateway 의 잔고 캐시 (5초) 가 보호하므로 동시에 여러 소비자가
    fallback 으로 떨어져도 KIS 로는 1건만 나간다. 둘 다 실패하면 None.
    """
    if redis_client is not None:
        snap = await read_balance_snapshot(redis_client, max_age_sec=max_age_sec)
        if snap is not None:
            return snap

    url = f"{gateway_url.rstrip('/')}/api/balance"
    try:
        if http is None:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url)
        else:
            resp = await http.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        logger.warning("balance HTTP fallback failed: %s", url, exc_info=True)
        return None


__all__ = [
    "DEFAULT_MAX_AGE_SEC",
    "LIVE_POSITIONS_KEY",
    "get_balance_snapshot_or_fetch",
    "read_balance_snapshot",
]
