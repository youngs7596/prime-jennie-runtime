"""Runtime state ↔ KIS 잔고 reconcile (read-only alert).

audit D1 fix (2026-05-15): 장중 외부 매수/매도 (KIS 앱, telegram /sell 의
v3 미반영 path 등) 가 v3 state 와 어긋난 상태로 누적되면:
  - fast_loop 가 없는 종목 monitor 시도 → KIS rate limit 위험
  - 같은 종목 새 sheet → already_holding reject (사용자 다시 매수 못 함)
  - executions/event_log 누락 → Phase 0 분석 왜곡

position_sync_check.py 가 같은 logic 을 **startup 1회** 만 검사. 본 job 은
5분 주기 runtime 검사 — alert 만, 자동 정리는 안 함 (feedback_sync_positions_manual
메모리 준수).

ticker 존재 여부뿐 아니라 **수량(quantity) 도 비교** — 양쪽에 ticker 가 다 있어도
수량이 어긋나면 (외부 일부 매도/매수) qty_mismatch 로 검출. ticker set 만 비교하던
구버전은 v3 sell 로 ticker 가 한쪽에서 사라질 때까지 drift 를 놓쳤다
(2026-05-18 241560 20주 갭 9일 지연 검출 학습).

자동 정리 안 하는 이유: 차이가 "오류 (외부 매도 발생)" 인지 "in-progress 상태
(주문 발행 중)" 인지 단순 검사로 판별 불가. 사용자 결정 보존.

alert debounce: 동일 mismatch 가 5분 주기마다 텔레그램 spam 을 내지 않도록
mismatch signature 별로 RECONCILE_ALERT_DEBOUNCE_SEC (기본 30분) debounce.
mismatch 내용이 바뀌면 (새 ticker / 수량 변화) signature 가 달라져 즉시 재알림
— 새 문제를 가리지 않는다. (2026-05-18 84회 spam 학습)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, datetime

import httpx
import redis.asyncio as aioredis

from prime_jennie_runtime.infra.acknowledged_holdings import acknowledged_untracked_tickers

logger = logging.getLogger(__name__)

POSITION_STATE_PREFIX = "position_state:"
ALERT_DEBOUNCE_KEY_PREFIX = "reconcile:alert:debounce:"
_ALERT_DEBOUNCE_SEC = int(os.environ.get("RECONCILE_ALERT_DEBOUNCE_SEC", "1800"))


async def _should_alert(
    redis: aioredis.Redis,
    only_in_state: list[str],
    only_in_kis: list[str],
    qty_mismatch: list[dict],
) -> bool:
    """동일 mismatch 의 연속 alert 를 debounce — SET NX 성공 시에만 alert.

    mismatch 3종을 직렬화한 signature 로 키를 만들고 ``SET NX EX`` 한다. window
    (``_ALERT_DEBOUNCE_SEC``) 내에 동일 signature 가 다시 오면 키가 이미 있어
    False. 내용이 바뀌면 signature 가 달라 새 키 → True (즉시 재알림).

    window <= 0 이면 debounce off (항상 True). debounce SET 자체가 실패하면
    fail-open — alert 를 잃지 않도록 True.
    """
    if _ALERT_DEBOUNCE_SEC <= 0:
        return True
    sig_src = json.dumps(
        {"s": only_in_state, "k": only_in_kis, "q": qty_mismatch},
        sort_keys=True,
        default=str,
    )
    sig = hashlib.sha1(sig_src.encode()).hexdigest()[:16]
    try:
        return bool(
            await redis.set(
                f"{ALERT_DEBOUNCE_KEY_PREFIX}{sig}", "1", nx=True, ex=_ALERT_DEBOUNCE_SEC
            )
        )
    except Exception:
        logger.warning("reconcile debounce SET 실패 — alert 진행 (fail-open)", exc_info=True)
        return True


async def _kis_balance_quantities(http: httpx.AsyncClient, gateway_url: str) -> dict[str, int]:
    """KIS gateway balance 호출 후 ``{ticker: quantity}`` 반환 (quantity > 0 만)."""
    resp = await http.get(f"{gateway_url.rstrip('/')}/api/balance", timeout=5.0)
    resp.raise_for_status()
    payload = resp.json()
    out: dict[str, int] = {}
    for p in payload.get("positions", []) or []:
        qty = int(p.get("quantity", 0) or 0)
        if qty > 0:
            out[str(p["stock_code"])] = qty
    return out


async def _redis_state_quantities(redis: aioredis.Redis) -> dict[str, int]:
    """position_state:* key 들에서 ``{ticker: 합산 quantity}`` 반환 (quantity > 0 만).

    같은 ticker 에 여러 state key (manual sync key + v3 sheet key 등) 가 존재하면
    합산한다 — v3 가 인식하는 그 ticker 의 총 보유 수량.
    """
    out: dict[str, int] = {}
    cursor: int = 0
    while True:
        cursor, keys = await redis.scan(cursor, match=f"{POSITION_STATE_PREFIX}*", count=100)
        for raw_key in keys:
            try:
                raw = await redis.get(raw_key)
                if raw is None:
                    continue
                data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                qty = int(data.get("quantity", 0) or 0)
                ticker = data.get("ticker")
                if qty > 0 and ticker:
                    out[str(ticker)] = out.get(str(ticker), 0) + qty
            except Exception:
                logger.exception("state parse failed key=%s", raw_key)
        if cursor == 0:
            break
    return out


async def reconcile_state_kis(
    *,
    redis_client: aioredis.Redis,
    http: httpx.AsyncClient,
    kis_gateway_url: str,
) -> dict[str, list]:
    """KIS balance vs v3 redis state 비교 + 차이 시 alert (notification stream).

    Returns:
        ``{"only_in_state": [...], "only_in_kis": [...], "qty_mismatch": [...]}``
        - only_in_state: KIS 잔고에는 없는데 v3 state 에 남은 ticker (외부 매도)
        - only_in_kis: v3 state 에 없는데 KIS 에 있는 ticker (외부 매수)
        - qty_mismatch: 양쪽에 다 있지만 수량이 다른 ticker. 각 원소는
          ``{"ticker", "state_qty", "kis_qty"}`` dict — 외부 일부 매도/매수.

    KIS / redis 장애 시 빈 dict 반환 + WARN log (fail-open).
    """
    try:
        kis = await _kis_balance_quantities(http, kis_gateway_url)
    except Exception:
        logger.warning("reconcile: balance fetch failed", exc_info=True)
        return {}

    try:
        state = await _redis_state_quantities(redis_client)
    except Exception:
        logger.warning("reconcile: redis scan failed", exc_info=True)
        return {}

    kis_tickers = set(kis)
    state_tickers = set(state)
    only_in_state = sorted(state_tickers - kis_tickers)
    only_in_kis = sorted(kis_tickers - state_tickers)
    # 사용자가 인지한 비추적 보유 (예: 069500 KODEX200 관리 제외, 2026-06-12)
    # 는 only_in_kis 경고에서만 제외 — 반대 방향·수량 drift 는 그대로 경고.
    acknowledged = acknowledged_untracked_tickers() & set(only_in_kis)
    if acknowledged:
        only_in_kis = [t for t in only_in_kis if t not in acknowledged]
        logger.info(
            "reconcile: 인지된 비추적 보유 %s — only_in_kis 경고에서 제외",
            sorted(acknowledged),
        )
    qty_mismatch = [
        {"ticker": t, "state_qty": state[t], "kis_qty": kis[t]}
        for t in sorted(state_tickers & kis_tickers)
        if state[t] != kis[t]
    ]

    if not only_in_state and not only_in_kis and not qty_mismatch:
        logger.info(
            "reconcile: state-KIS aligned (%d tickers)",
            len(state_tickers),
        )
        return {"only_in_state": [], "only_in_kis": [], "qty_mismatch": []}

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
    if qty_mismatch:
        detail = ", ".join(
            f"{m['ticker']}(state={m['state_qty']} kis={m['kis_qty']})" for m in qty_mismatch
        )
        body_parts.append(
            f"qty_mismatch ({len(qty_mismatch)}): {detail} — 수량 drift. "
            f"외부 일부 매도/매수 가능성."
        )

    # v3 가 실제(KIS)보다 많이 보유 중이라 착각하는 경우 — only_in_state 또는
    # state_qty > kis_qty — 는 phantom 청산/oversell 위험이라 critical.
    # 그 외 (외부 매수 미추적 / v3 과소 카운트) 는 warning.
    state_overcount = any(m["state_qty"] > m["kis_qty"] for m in qty_mismatch)
    severity = "critical" if (only_in_state or state_overcount) else "warning"

    logger.warning(
        "reconcile mismatch: only_in_state=%s only_in_kis=%s qty_mismatch=%s",
        only_in_state,
        only_in_kis,
        qty_mismatch,
    )

    # 동일 mismatch 연속 alert spam 차단 — 내용이 바뀌면 즉시 재알림.
    result = {
        "only_in_state": only_in_state,
        "only_in_kis": only_in_kis,
        "qty_mismatch": qty_mismatch,
    }
    if not await _should_alert(redis_client, only_in_state, only_in_kis, qty_mismatch):
        logger.info("reconcile: 동일 mismatch — alert debounce 로 publish skip")
        return result

    payload = {
        "kind": "alert",
        "severity": severity,
        "title": "runtime state-KIS mismatch",
        "body": " | ".join(body_parts),
        "ts": datetime.now(UTC).isoformat(),
        "metadata": {
            "component": "positions_reconcile",
            "only_in_state": only_in_state,
            "only_in_kis": only_in_kis,
            "qty_mismatch": qty_mismatch,
        },
    }
    try:
        await redis_client.xadd(
            "v3:notifications", {"payload": json.dumps(payload, default=str)}, maxlen=10_000
        )
    except Exception:
        logger.exception("reconcile alert publish failed")

    return result
