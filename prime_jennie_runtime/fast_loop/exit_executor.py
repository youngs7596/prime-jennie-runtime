"""Exit Executor — ExitDecision을 실제 매도 주문으로 실행.

청산 (`should_close=True`): 시장가 전량 or scale_out 부분 수량
SL 상향 (`new_sl_price`): 주문 안 함, state만 갱신 (breakeven_sl_price)

v2 `services/seller/executor.py` (344줄) 구조 참고. v3는 시트 기반이라
per-sheet 수량/상태 관리.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from prime_jennie_runtime.control.state import SystemState
from prime_jennie_runtime.fast_loop.domain import ExitDecision, PositionState
from prime_jennie_runtime.fast_loop.kis_client import KisClient
from prime_jennie_runtime.fast_loop.notifier import Notifier
from prime_jennie_runtime.fast_loop.persistence import (
    NoopTradeRecorder,
    StockNameResolver,
    TradeRecorder,
)
from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker
from prime_jennie_runtime.fast_loop.schemas import GenericAlertNotification, TradeNotification
from prime_jennie_runtime.kis_gateway.schemas import OrderRequest
from prime_jennie_runtime.position_sheet.schema import PositionSheet

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
        recorder: TradeRecorder | None = None,
        sheet_fetcher: Callable[[str], Awaitable[list[PositionSheet]]] | None = None,
        system_state: SystemState | None = None,
        stock_resolver: StockNameResolver | None = None,
        *,
        max_confirm_retries: int = 5,
        confirm_interval: float = 2.0,
        max_exit_failures: int = 3,
    ):
        self._kis = kis
        self._tracker = tracker
        self._notifier = notifier
        self._recorder: TradeRecorder = recorder or NoopTradeRecorder()
        self._sheet_fetcher = sheet_fetcher
        self._system_state = system_state
        self._stock_resolver = stock_resolver
        self._max_retries = max_confirm_retries
        self._confirm_interval = confirm_interval
        # 연속 매도 실패 N회 도달 시 sheet auto-close (5-13 stale state 사고 차단)
        self._max_exit_failures = max_exit_failures

    async def _resolve_name(self, ticker: str) -> str:
        if self._stock_resolver is None:
            return ""
        try:
            return await self._stock_resolver(ticker)
        except Exception:
            logger.debug("stock_resolver raised for %s", ticker)
            return ""

    async def _record_exit_failure(
        self, state: PositionState, reason: str, failure_kind: str
    ) -> None:
        """매도 실패 누적 — threshold 도달 시 sheet auto-close + alert.

        외부 청산 (사용자 수동, HTS) 으로 KIS 잔고가 0 이 된 sheet 가 매 tick
        마다 무한 매도 시도 → KIS rate limit 폭주 사고 차단 (2026-05-13).
        """
        state.exit_fail_count += 1
        logger.warning(
            "exit failure recorded: sheet=%s ticker=%s kind=%s reason=%s fail_count=%d/%d",
            state.sheet_id,
            state.ticker,
            failure_kind,
            reason,
            state.exit_fail_count,
            self._max_exit_failures,
        )

        if state.exit_fail_count < self._max_exit_failures:
            await self._tracker.persist(state.sheet_id)
            return

        logger.error(
            "exit abandoned after %d failures: sheet=%s ticker=%s — closing sheet",
            state.exit_fail_count,
            state.sheet_id,
            state.ticker,
        )
        await self._tracker.close(state.sheet_id)
        await self._notifier.emit(
            GenericAlertNotification(
                severity="critical",
                title="exit abandoned",
                body=(
                    f"{state.ticker} {state.sheet_id} 매도 {state.exit_fail_count}회 "
                    f"연속 실패 ({failure_kind}) — sheet auto-close. "
                    f"KIS 잔고 확인 후 수동 정정 필요."
                ),
                ts=datetime.now(UTC),
                metadata={
                    "sheet_id": state.sheet_id,
                    "ticker": state.ticker,
                    "fail_count": state.exit_fail_count,
                    "last_failure_kind": failure_kind,
                    "last_reason": reason,
                },
            )
        )

    @staticmethod
    def _compute_pnl(
        entry_price: float, filled_price: float, qty: int
    ) -> tuple[float | None, float | None]:
        """청산 손익 계산. entry_price 또는 filled_price 가 0 이하면 None 반환 (DRY_RUN 등)."""
        if entry_price <= 0 or filled_price <= 0 or qty <= 0:
            return None, None
        pnl_amount = (filled_price - entry_price) * qty
        pnl_pct = (filled_price / entry_price - 1.0) * 100.0
        return pnl_amount, pnl_pct

    async def execute(self, state: PositionState, decision: ExitDecision) -> ExitOutcome:
        """decision에 따라 sell 주문 또는 SL 상향만.

        - should_close=False + new_sl_price: breakeven_sl_price 갱신만, 주문 X
        - should_close=True + portion=1.0: 전량 시장가
        - should_close=True + portion<1.0: 부분 시장가 (scale_out)

        ``system_state.stopped`` 가 True 면 (긴급정지) sell 주문 발행 없이 skip —
        STOP 명령 의 설계 의도 ("진입/청산 모두 중단") 대로 청산 path 도 봉쇄.
        SL 상향 같은 주문 미동반 결정은 stop 영향 없이 진행.
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

        if self._system_state is not None:
            snap = await self._system_state.snapshot()
            if snap.stopped:
                logger.warning(
                    "exit blocked by STOP: sheet=%s ticker=%s reason=%s",
                    state.sheet_id,
                    state.ticker,
                    decision.reason,
                )
                return ExitOutcome(
                    success=False,
                    sheet_id=state.sheet_id,
                    ticker=state.ticker,
                    reason=f"blocked_stop:{decision.reason}",
                )
            if snap.dryrun:
                qty_dry = max(1, int(state.quantity * decision.portion))
                qty_dry = min(qty_dry, state.quantity)
                now_dry = datetime.now(UTC)
                logger.warning(
                    "exit DRY_RUN: skipping KIS sheet=%s ticker=%s qty=%d reason=%s",
                    state.sheet_id,
                    state.ticker,
                    qty_dry,
                    decision.reason,
                )
                await self._notifier.emit(
                    TradeNotification(
                        kind="exit_filled",
                        sheet_id=state.sheet_id,
                        ticker=state.ticker,
                        stock_name=await self._resolve_name(state.ticker),
                        side="sell",
                        quantity=qty_dry,
                        price=0.0,
                        ts=now_dry,
                        reason=f"dryrun:{decision.reason}",
                        entry_avg_price=state.entry_price,
                    )
                )
                return ExitOutcome(
                    success=True,
                    sheet_id=state.sheet_id,
                    ticker=state.ticker,
                    closed_qty=qty_dry,
                    avg_price=0.0,
                    reason=f"dryrun:{decision.reason}",
                    fully_closed=qty_dry >= state.quantity,
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
            await self._record_exit_failure(
                state, decision.reason, result.message or "sell_rejected"
            )
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
            await self._record_exit_failure(state, decision.reason, "sell_not_filled")
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

        # 성공 매도 — fail count 리셋
        state.exit_fail_count = 0

        if fully_closed:
            await self._tracker.close(state.sheet_id)
        else:
            await self._tracker.persist(state.sheet_id)

        # PG persist — executions INSERT + positions UPDATE/DELETE
        if self._sheet_fetcher is not None:
            sheets = await self._sheet_fetcher(state.sheet_id)
            if sheets:
                await self._recorder.record_sell(
                    sheets[0],
                    filled_qty=filled_qty,
                    filled_price=filled_price,
                    executed_at=now,
                    reason=decision.reason,
                )

        pnl_amount, pnl_pct = self._compute_pnl(state.entry_price, filled_price, filled_qty)
        await self._notifier.emit(
            TradeNotification(
                kind="exit_filled",
                sheet_id=state.sheet_id,
                ticker=state.ticker,
                stock_name=await self._resolve_name(state.ticker),
                side="sell",
                quantity=filled_qty,
                price=filled_price,
                ts=now,
                reason=decision.reason,
                is_partial=is_partial,
                entry_avg_price=state.entry_price if state.entry_price > 0 else None,
                pnl_amount=pnl_amount,
                pnl_pct=pnl_pct,
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
