"""Entry Executor — PositionSheet의 entry 섹션을 실제 주문으로 실행.

시트 수신 후:
1. 장 시간 + emergency_stop 확인 (호출자 측에서도 선행 체크 가능)
2. OrderRequest 생성 (limit/market)
3. KIS Gateway → order_no 수신
4. confirm_order 폴링으로 체결 확인
5. 체결 성공 → PositionState 생성 + tracker.register + notifier.emit
6. 미체결 → 취소 + rejected 이벤트

v2 `services/buyer/executor.py` (546줄)의 `_place_order` / `_wait_for_fill`
로직 구조를 참고하되, 시트 기반으로 재작성. portfolio guard 등 pre-check는
Slow loop가 이미 처리.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from prime_jennie_runtime.control.state import SystemState
from prime_jennie_runtime.fast_loop.domain import PositionState
from prime_jennie_runtime.fast_loop.kis_client import KisClient
from prime_jennie_runtime.fast_loop.notifier import Notifier
from prime_jennie_runtime.fast_loop.persistence import (
    NoopTradeRecorder,
    StockNameResolver,
    TradeRecorder,
)
from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker
from prime_jennie_runtime.fast_loop.schemas import TradeNotification
from prime_jennie_runtime.kis_gateway.schemas import OrderRequest
from prime_jennie_runtime.position_sheet.schema import PositionSheet

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EntryOutcome:
    """entry 실행 결과."""

    sheet_id: str
    ticker: str
    success: bool
    order_no: str | None = None
    filled_qty: int = 0
    filled_price: float = 0.0
    reason: str = ""  # 실패 시 사유


class EntryExecutor:
    """시트 → entry 주문 + tracker 등록."""

    def __init__(
        self,
        kis: KisClient,
        tracker: PositionTracker,
        notifier: Notifier,
        recorder: TradeRecorder | None = None,
        system_state: SystemState | None = None,
        stock_resolver: StockNameResolver | None = None,
        *,
        max_confirm_retries: int = 5,
        confirm_interval: float = 3.0,
    ):
        self._kis = kis
        self._tracker = tracker
        self._notifier = notifier
        self._recorder: TradeRecorder = recorder or NoopTradeRecorder()
        self._system_state = system_state
        self._stock_resolver = stock_resolver
        self._max_retries = max_confirm_retries
        self._confirm_interval = confirm_interval

    async def _resolve_name(self, ticker: str) -> str:
        if self._stock_resolver is None:
            return ""
        try:
            return await self._stock_resolver(ticker)
        except Exception:
            logger.debug("stock_resolver raised for %s", ticker)
            return ""

    async def execute(self, sheet: PositionSheet, *, quantity: int) -> EntryOutcome:
        """주문 제출 → 체결 확인 → 추적 등록. 실패 시 취소.

        quantity는 호출자가 시트의 final_pct + 계좌 잔고에서 계산해서 전달.
        (sheet만으로는 quantity 정해지지 않음 — final_pct는 비율일 뿐)

        DRY_RUN 키 ON 이면 KIS 호출 없이 시뮬레이션 outcome 반환 +
        notifier 에 dryrun 알림.
        """
        order = _build_order_request(sheet, quantity)
        if order is None:
            return EntryOutcome(
                sheet_id=sheet.sheet_id,
                ticker=sheet.ticker,
                success=False,
                reason="invalid_order_request",
            )

        if self._system_state is not None:
            snap = await self._system_state.snapshot()
            if snap.dryrun:
                now = datetime.now(UTC)
                logger.warning(
                    "entry DRY_RUN: skipping KIS sheet=%s ticker=%s qty=%d",
                    sheet.sheet_id,
                    sheet.ticker,
                    quantity,
                )
                await self._notifier.emit(
                    TradeNotification(
                        kind="entry_filled",
                        sheet_id=sheet.sheet_id,
                        ticker=sheet.ticker,
                        stock_name=await self._resolve_name(sheet.ticker),
                        side="buy",
                        quantity=quantity,
                        price=0.0,
                        ts=now,
                        reason="dryrun",
                    )
                )
                return EntryOutcome(
                    sheet_id=sheet.sheet_id,
                    ticker=sheet.ticker,
                    success=True,
                    filled_qty=quantity,
                    filled_price=0.0,
                    reason="dryrun",
                )

        logger.info(
            "entry submitting sheet=%s ticker=%s qty=%d type=%s price=%s",
            sheet.sheet_id,
            sheet.ticker,
            quantity,
            order.order_type,
            order.price,
        )
        result = await self._kis.buy(order)
        if not result.success or not result.order_no:
            return EntryOutcome(
                sheet_id=sheet.sheet_id,
                ticker=sheet.ticker,
                success=False,
                reason=result.message or "buy_rejected",
            )

        status = await self._kis.confirm_order(
            result.order_no,
            max_retries=self._max_retries,
            interval=self._confirm_interval,
        )

        if status is None or status.filled_qty <= 0:
            # 미체결 → 취소 시도
            try:
                await self._kis.cancel_order(result.order_no)
            except Exception:
                logger.exception("cancel_order failed for %s", result.order_no)
            return EntryOutcome(
                sheet_id=sheet.sheet_id,
                ticker=sheet.ticker,
                success=False,
                order_no=result.order_no,
                reason="not_filled",
            )

        filled_qty = status.filled_qty
        filled_price = status.avg_price

        # 추적 등록
        now = datetime.now(UTC)
        state = PositionState(
            sheet_id=sheet.sheet_id,
            ticker=sheet.ticker,
            entry_price=filled_price,
            quantity=filled_qty,
            entered_at=now,
            high_watermark=filled_price,
        )
        await self._tracker.register(state)

        # PG persist (executions + positions)
        await self._recorder.record_buy(
            sheet,
            filled_qty=filled_qty,
            filled_price=filled_price,
            executed_at=now,
        )

        # 알림 발행
        await self._notifier.emit(
            TradeNotification(
                kind="entry_filled",
                sheet_id=sheet.sheet_id,
                ticker=sheet.ticker,
                stock_name=await self._resolve_name(sheet.ticker),
                side="buy",
                quantity=filled_qty,
                price=filled_price,
                ts=now,
                is_partial=filled_qty < quantity,
            )
        )

        return EntryOutcome(
            sheet_id=sheet.sheet_id,
            ticker=sheet.ticker,
            success=True,
            order_no=result.order_no,
            filled_qty=filled_qty,
            filled_price=filled_price,
        )


# ─── helpers ─────────────────────────────────────────────


def _build_order_request(sheet: PositionSheet, quantity: int) -> OrderRequest | None:
    if quantity <= 0:
        logger.warning("entry quantity <= 0 for sheet=%s", sheet.sheet_id)
        return None

    trigger = sheet.entry.trigger
    if trigger == "market":
        return OrderRequest(
            stock_code=sheet.ticker,
            quantity=quantity,
            order_type="market",
        )

    # limit
    if sheet.entry.price is None or sheet.entry.price <= 0:
        logger.warning("limit entry but no price for sheet=%s", sheet.sheet_id)
        return None
    return OrderRequest(
        stock_code=sheet.ticker,
        quantity=quantity,
        order_type="limit",
        price=_align_tick_size(int(sheet.entry.price)),
    )


def _align_tick_size(price: int) -> int:
    """KRX 호가 단위 정렬. v2 `buyer/executor.py` 포팅 (L520)."""
    if price < 2000:
        return price
    if price < 5000:
        return (price // 5) * 5
    if price < 20000:
        return (price // 10) * 10
    if price < 50000:
        return (price // 50) * 50
    if price < 200000:
        return (price // 100) * 100
    if price < 500000:
        return (price // 500) * 500
    return (price // 1000) * 1000
