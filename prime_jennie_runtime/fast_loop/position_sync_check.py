"""Startup KIS-position mismatch 검사기.

부팅 시 ``PositionTracker.load_from_redis()`` 직후, KIS 실잔고 와 in-memory
state 를 비교해 외부 청산 (사용자 수동, HTS 직거래) 으로 인한 stale state
를 탐지하여 텔레그램 / Slack 알림으로 즉시 인지시킨다.

**자동 정리 안 함** — 사용자 메모리 ``feedback_sync_positions_manual.md`` 준수.
운영자가 알림 확인 후 수동으로 sheet close 또는 redis state 삭제 결정.

5-13 사고 학습: stale state 가 매 tick 마다 무한 매도 시도 → KIS rate limit
폭주 → fast_loop crash 사이클. 부팅 시 mismatch 감지가 첫 방어선.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from prime_jennie_runtime.fast_loop.kis_client import KisClient
from prime_jennie_runtime.fast_loop.notifier import Notifier
from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker
from prime_jennie_runtime.fast_loop.schemas import GenericAlertNotification

logger = logging.getLogger(__name__)


async def check_state_kis_mismatch(
    tracker: PositionTracker,
    kis: KisClient,
    notifier: Notifier,
) -> dict[str, list[str]]:
    """tracker in-memory state 와 KIS balance ticker 차이를 산출 + 알림.

    Returns: ``{"only_in_state": [...], "only_in_kis": [...]}``
        - only_in_state: KIS 잔고에는 없는데 fast_loop state 에 남은 ticker 목록
          (가장 위험 — 무한 매도 시도 후보)
        - only_in_kis: fast_loop state 에는 없는데 KIS 에 있는 ticker 목록
          (덜 위험 — exit_evaluator 가 미작동, 모니터링 누락)

    KIS 호출 실패 시 빈 dict 반환 + WARN 로깅. fast_loop 부팅 자체는 막지 않음.
    """
    try:
        portfolio = await kis.get_balance()
    except Exception:
        logger.warning(
            "startup state-KIS mismatch check skipped — balance fetch failed", exc_info=True
        )
        return {}

    kis_tickers = {pos.stock_code for pos in portfolio.positions if pos.quantity > 0}
    state_tickers = set(tracker.tickers_active())

    only_in_state = sorted(state_tickers - kis_tickers)
    only_in_kis = sorted(kis_tickers - state_tickers)

    if not only_in_state and not only_in_kis:
        logger.info("startup state-KIS check OK: %d tickers matched", len(state_tickers))
        return {"only_in_state": [], "only_in_kis": []}

    sheet_details: list[str] = []
    for ticker in only_in_state:
        sheet_ids = tracker.sheet_ids_for(ticker)
        sheet_details.append(f"{ticker}({', '.join(sheet_ids)})")

    body_parts = []
    if only_in_state:
        body_parts.append(
            f"state-only ({len(only_in_state)}): {', '.join(sheet_details)} — "
            "외부 청산 가능성, exit_executor 가 무한 매도 시도할 수 있음"
        )
    if only_in_kis:
        body_parts.append(
            f"kis-only ({len(only_in_kis)}): {', '.join(only_in_kis)} — "
            "fast_loop 가 추적하지 않는 보유 종목"
        )

    logger.warning(
        "startup state-KIS mismatch detected: only_in_state=%s only_in_kis=%s",
        only_in_state,
        only_in_kis,
    )

    await notifier.emit(
        GenericAlertNotification(
            severity="critical" if only_in_state else "warning",
            title="startup state-KIS mismatch",
            body=" | ".join(body_parts),
            ts=datetime.now(UTC),
            metadata={
                "only_in_state": only_in_state,
                "only_in_kis": only_in_kis,
            },
        )
    )
    return {"only_in_state": only_in_state, "only_in_kis": only_in_kis}


__all__ = ["check_state_kis_mismatch"]
