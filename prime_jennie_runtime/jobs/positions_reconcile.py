"""Runtime state ↔ KIS 잔고 reconcile (read-only alert).

audit D1 fix (2026-05-15): 장중 외부 매수/매도 (KIS 앱, telegram /sell 의
v3 미반영 path 등) 가 v3 state 와 어긋난 상태로 누적되면:
  - fast_loop 가 없는 종목 monitor 시도 → KIS rate limit 위험
  - 같은 종목 새 sheet → already_holding reject (사용자 다시 매수 못 함)
  - executions/event_log 누락 → Phase 0 분석 왜곡

position_sync_check.py 가 같은 logic 을 **startup 1회** 만 검사. 본 job 은
5분 주기 runtime 검사 — alert 만, 자동 정리는 안 함 (feedback_sync_positions_manual
메모리 준수).

자동 정리 안 하는 이유: 차이가 "오류 (외부 매도 발생)" 인지 "in-progress 상태
(주문 발행 중)" 인지 단순 검사로 판별 불가. 사용자 결정 보존.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import httpx
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

POSITION_STATE_PREFIX = "position_state:"


async def _kis_balance_tickers(http: httpx.AsyncClient, gateway_url: str) -> set[str]:
    """KIS gateway balance 호출 후 quantity > 0 ticker set 반환."""
    resp = await http.get(f"{gateway_url.rstrip('/')}/api/balance", timeout=5.0)
    resp.raise_for_status()
    payload = resp.json()
    return {
        str(p["stock_code"])
        for p in payload.get("positions", []) or []
        if int(p.get("quantity", 0)) > 0
    }


async def _redis_state_tickers(redis: aioredis.Redis) -> set[str]:
    """redis 의 position_state:* key 들에서 quantity > 0 ticker set 반환."""
    tickers: set[str] = set()
    cursor: int = 0
    while True:
        cursor, keys = await redis.scan(cursor, match=f"{POSITION_STATE_PREFIX}*", count=100)
        for raw_key in keys:
            try:
                raw = await redis.get(raw_key)
                if raw is None:
                    continue
                data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                if int(data.get("quantity", 0)) > 0 and data.get("ticker"):
                    tickers.add(str(data["ticker"]))
            except Exception:
                logger.exception("state parse failed key=%s", raw_key)
        if cursor == 0:
            break
    return tickers


async def reconcile_state_kis(
    *,
    redis_client: aioredis.Redis,
    http: httpx.AsyncClient,
    kis_gateway_url: str,
) -> dict[str, list[str]]:
    """KIS balance vs v3 redis state 비교 + 차이 시 alert (notification stream).

    Returns:
        ``{"only_in_state": [...], "only_in_kis": [...]}``
        - only_in_state: KIS 잔고에는 없는데 v3 state 에 남은 ticker (외부 매도)
        - only_in_kis: v3 state 에 없는데 KIS 에 있는 ticker (외부 매수)

    KIS / redis 장애 시 빈 dict 반환 + WARN log (fail-open).
    """
    try:
        kis_tickers = await _kis_balance_tickers(http, kis_gateway_url)
    except Exception:
        logger.warning("reconcile: balance fetch failed", exc_info=True)
        return {}

    try:
        state_tickers = await _redis_state_tickers(redis_client)
    except Exception:
        logger.warning("reconcile: redis scan failed", exc_info=True)
        return {}

    only_in_state = sorted(state_tickers - kis_tickers)
    only_in_kis = sorted(kis_tickers - state_tickers)

    if not only_in_state and not only_in_kis:
        logger.info(
            "reconcile: state-KIS aligned (%d tickers)",
            len(state_tickers),
        )
        return {"only_in_state": [], "only_in_kis": []}

    # Notifier 없어도 stream 에 직접 publish (jobs/app.py 의 다른 alert 와 동일 패턴).
    body_parts: list[str] = []
    if only_in_state:
        body_parts.append(
            f"only_in_state ({len(only_in_state)}): {', '.join(only_in_state)} — "
            f"외부 매도 가능성. v3 가 보유 중이라 착각."
        )
    if only_in_kis:
        body_parts.append(
            f"only_in_kis ({len(only_in_kis)}): {', '.join(only_in_kis)} — "
            f"외부 매수 가능성. v3 미추적."
        )

    logger.warning(
        "reconcile mismatch: only_in_state=%s only_in_kis=%s",
        only_in_state,
        only_in_kis,
    )

    payload = {
        "kind": "alert",
        "severity": "critical" if only_in_state else "warning",
        "title": "runtime state-KIS mismatch",
        "body": " | ".join(body_parts),
        "ts": datetime.now(UTC).isoformat(),
        "metadata": {
            "component": "positions_reconcile",
            "only_in_state": only_in_state,
            "only_in_kis": only_in_kis,
        },
    }
    try:
        await redis_client.xadd(
            "v3:notifications", {"payload": json.dumps(payload, default=str)}, maxlen=10_000
        )
    except Exception:
        logger.exception("reconcile alert publish failed")

    return {"only_in_state": only_in_state, "only_in_kis": only_in_kis}
