"""Fast Loop 테스트용 페이크 객체들."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from prime_jennie_runtime.kis_gateway.schemas import (
    OrderRequest,
    OrderResult,
    OrderStatusResult,
    PortfolioState,
    StockSnapshot,
)


@dataclass
class FakeKisClient:
    """KisClient 대체. 주문은 in-memory order_no 채번, fill은 미리 지정."""

    fill_price: float = 70000.0
    fill_qty_override: dict[str, int] = field(default_factory=dict)
    # confirm_order 가 check_order_status 와 다른 값을 반환하도록 — 부분 체결
    # (confirm 시점 체결량 < 주문량) 시뮬레이션용.
    confirm_qty_override: dict[str, int] = field(default_factory=dict)
    should_fill: bool = True
    _counter: int = 0
    buy_calls: list[OrderRequest] = field(default_factory=list)
    sell_calls: list[OrderRequest] = field(default_factory=list)
    cancel_calls: list[str] = field(default_factory=list)
    subscribe_calls: list[list[str]] = field(default_factory=list)
    unsubscribe_calls: list[list[str]] = field(default_factory=list)

    async def buy(self, order: OrderRequest) -> OrderResult:
        self._counter += 1
        self.buy_calls.append(order)
        return OrderResult(
            success=True,
            order_no=f"BUY{self._counter:06d}",
            stock_code=order.stock_code,
            quantity=order.quantity,
            price=order.price or int(self.fill_price),
            message=None,
        )

    async def sell(self, order: OrderRequest) -> OrderResult:
        self._counter += 1
        self.sell_calls.append(order)
        return OrderResult(
            success=True,
            order_no=f"SELL{self._counter:06d}",
            stock_code=order.stock_code,
            quantity=order.quantity,
            price=int(self.fill_price),
            message=None,
        )

    async def cancel_order(self, order_no: str) -> bool:
        self.cancel_calls.append(order_no)
        return True

    async def check_order_status(self, order_no: str) -> OrderStatusResult | None:
        qty = self.fill_qty_override.get(order_no)
        if not self.should_fill:
            return OrderStatusResult(filled=False, filled_qty=0, avg_price=0.0)
        if qty is None:
            qty = (
                self.buy_calls[-1].quantity
                if order_no.startswith("BUY")
                else (self.sell_calls[-1].quantity if self.sell_calls else 0)
            )
        return OrderStatusResult(filled=True, filled_qty=qty, avg_price=self.fill_price)

    async def confirm_order(
        self,
        order_no: str,
        *,
        max_retries: int = 5,
        interval: float = 3.0,
    ) -> OrderStatusResult | None:
        # confirm_qty_override 지정 시 부분 체결 시뮬레이션 — confirm_order 가
        # check_order_status (취소 후 재조회) 와 다른 체결량을 반환하게 한다.
        qty = self.confirm_qty_override.get(order_no)
        if qty is not None:
            ordered = (
                self.buy_calls[-1].quantity
                if order_no.startswith("BUY")
                else (self.sell_calls[-1].quantity if self.sell_calls else qty)
            )
            return OrderStatusResult(
                filled=qty >= ordered, filled_qty=qty, avg_price=self.fill_price
            )
        return await self.check_order_status(order_no)

    async def get_balance(self) -> PortfolioState:
        from datetime import UTC, datetime

        return PortfolioState(
            positions=[],
            cash_balance=10_000_000,
            total_asset=10_000_000,
            stock_eval_amount=0,
            position_count=0,
            timestamp=datetime.now(UTC),
        )

    async def get_cash(self) -> int:
        return 10_000_000

    async def get_snapshot(self, stock_code: str) -> StockSnapshot:
        from datetime import UTC, datetime

        return StockSnapshot(
            stock_code=stock_code,
            price=int(self.fill_price),
            timestamp=datetime.now(UTC),
        )

    async def subscribe(self, codes: list[str]) -> dict[str, Any]:
        self.subscribe_calls.append(list(codes))
        return {"success": True, "codes": codes}

    async def unsubscribe(self, codes: list[str]) -> dict[str, Any]:
        self.unsubscribe_calls.append(list(codes))
        return {"success": True, "codes": codes}

    async def close(self) -> None:
        pass
