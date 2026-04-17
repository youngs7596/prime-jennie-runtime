"""Exit Executor — ExitDecision을 실제 매도 주문으로 실행.

청산 (`should_close=True`): 시장가 전량 or scale_out 부분 수량
SL 상향 (`new_sl_price`): 주문 안 함, state만 갱신 (breakeven_sl_price)

v2 `services/seller/executor.py` (344줄) 구조 참고. v3는 시트 기반이라
per-sheet 수량/상태 관리.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from prime_jennie_runtime.fast_loop.domain import ExitDecision, PositionState
from prime_jennie_runtime.fast_loop.kis_client import KisClient
from prime_jennie_runtime.fast_loop.notifier import Notifier
from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker
from prime_jennie_runtime.fast_loop.schemas import TradeNotification
from prime_jennie_runtime.kis_gateway.schemas import OrderRequest

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExitOutcome:
    success: bool
    sheet_id: str
    ticker: str
    closed_qty: int = 0
    avg_price: float = 0.0
    reason: str = ""
    fully_closed: bool = False


class ExitExecutor:
    """ExitDecision → sell 주문."""

    def __init__(
        self,
        kis: KisClient,
        tracker: PositionTracker,
        notifier: Notifier,
        *,
        max_confirm_retries: int = 5,
        confirm_interval: float = 2.0,
    ):
        self._kis = kis
        self._tracker = tracker
        self._notifier = notifier
        self._max_retries = max_confirm_retries
        self._confirm_interval = confirm_interval

    async def execute(self, state: PositionState, decision: ExitDecision) -> ExitOutcome:
        """decision에 따라 sell 주문 또는 SL 상향만.

        - should_close=False + new_sl_price: breakeven_sl_price 갱신만, 주문 X
        - should_close=True + portion=1.0: 전량 시장가
        - should_close=True + portion<1.0: 부분 시장가 (scale_out)
        """
        if not decision.should_close:
            # SL 상향 보조 rule (breakeven_sl_raise)
            if decision.new_sl_price is not None:
                state.breakeven_sl_price = decision.new_sl_price
                await self._tracker.persist(state.sheet_id)
            return ExitOutcome(
                success=True,
                sheet_id=state.sheet_id,
                ticker=state.ticker,
                reason=decision.reason,
            )

        # sell 주문
        remaining = state.quantity
        qty_to_sell = max(1, int(remaining * decision.portion))
        qty_to_sell = min(qty_to_sell, remaining)

        order = OrderRequest(
            stock_code=state.ticker,
            quantity=qty_to_sell,
            order_type="market",
        )
        logger.info(
            "exit submitting sheet=%s ticker=%s qty=%d reason=%s",
            state.sheet_id,
            state.ticker,
            qty_to_sell,
            decision.reason,
        )
        result = await self._kis.sell(order)
        if not result.success or not result.order_no:
            return ExitOutcome(
                success=False,
                sheet_id=state.sheet_id,
                ticker=state.ticker,
                reason=result.message or "sell_rejected",
            )

        status = await self._kis.confirm_order(
            result.order_no,
            max_retries=self._max_retries,
            interval=self._confirm_interval,
        )

        if status is None or status.filled_qty <= 0:
            return ExitOutcome(
                success=False,
                sheet_id=state.sheet_id,
                ticker=state.ticker,
                reason="sell_not_filled",
            )

        filled_qty = status.filled_qty
        filled_price = status.avg_price
        now = datetime.now(UTC)

        # 부분 청산인지 여부 판단
        state.quantity -= filled_qty
        fully_closed = state.quantity <= 0 or decision.portion >= 1.0
        is_partial = not fully_closed

        if fully_closed:
            await self._tracker.close(state.sheet_id)
        else:
            await self._tracker.persist(state.sheet_id)

        await self._notifier.emit(
            TradeNotification(
                kind="exit_filled",
                sheet_id=state.sheet_id,
                ticker=state.ticker,
                side="sell",
                quantity=filled_qty,
                price=filled_price,
                ts=now,
                reason=decision.reason,
                is_partial=is_partial,
            )
        )

        return ExitOutcome(
            success=True,
            sheet_id=state.sheet_id,
            ticker=state.ticker,
            closed_qty=filled_qty,
            avg_price=filled_price,
            reason=decision.reason,
            fully_closed=fully_closed,
        )
