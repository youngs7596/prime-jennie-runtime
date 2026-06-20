"""user_directed_dca 테스트용 페이크 — 제어 가능한 KIS + 인메모리 repository.

InMemoryDcaRepo 는 DcaRepository 와 같은 메서드 시그니처를 구현해 PG 없이 tick·
executor 전체 흐름을 돌린다(덕 타이핑).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from prime_jennie_runtime.kis_gateway.schemas import (
    DailyExecution,
    OrderRequest,
    OrderResult,
    OrderStatusResult,
    StockSnapshot,
)
from prime_jennie_runtime.user_directed_dca.state import Campaign, SliceExecution


class FakeKis:
    """제어 가능한 KIS 게이트웨이 클라이언트 대역."""

    def __init__(
        self,
        *,
        price: int = 80_000,
        open_price: int = 80_000,
        change_pct: float = 0.0,
        cash: int = 300_000_000,
    ) -> None:
        self.price = price
        self.open_price = open_price
        self.change_pct = change_pct
        self.cash = cash
        self.reject = False  # buy → success=False (VI 거부 시뮬)
        self.buy_exception = False  # buy → raise
        self.fill_qty: int | None = None  # None=전량, int=부분
        self.confirm_returns_none = False  # 미체결
        self.daily_execs: list[DailyExecution] = []
        self.buy_calls: list[OrderRequest] = []
        self.cancel_calls: list[str] = []
        self._counter = 0
        self._last: OrderRequest | None = None

    async def get_cash(self) -> int:
        return self.cash

    async def get_snapshot(self, stock_code: str) -> StockSnapshot:
        return StockSnapshot(
            stock_code=stock_code,
            price=self.price,
            open_price=self.open_price,
            change_pct=self.change_pct,
            timestamp=datetime.now(UTC),
        )

    async def buy(self, order: OrderRequest) -> OrderResult:
        self.buy_calls.append(order)
        if self.buy_exception:
            raise RuntimeError("buy boom")
        if self.reject:
            return OrderResult(
                success=False,
                order_no=None,
                stock_code=order.stock_code,
                quantity=order.quantity,
                price=0,
                message="VI 거부",
            )
        self._counter += 1
        self._last = order
        return OrderResult(
            success=True,
            order_no=f"BUY{self._counter:06d}",
            stock_code=order.stock_code,
            quantity=order.quantity,
            price=self.price,
            message=None,
        )

    def _terminal_qty(self) -> int:
        ordered = self._last.quantity if self._last else 0
        return self.fill_qty if self.fill_qty is not None else ordered

    async def confirm_order(
        self, order_no: str, *, max_retries: int = 5, interval: float = 3.0
    ) -> OrderStatusResult | None:
        if self.confirm_returns_none:
            return None
        ordered = self._last.quantity if self._last else 0
        qty = self._terminal_qty()
        return OrderStatusResult(filled=qty >= ordered, filled_qty=qty, avg_price=float(self.price))

    async def check_order_status(self, order_no: str) -> OrderStatusResult | None:
        if self.confirm_returns_none:
            return OrderStatusResult(filled=False, filled_qty=0, avg_price=0.0)
        ordered = self._last.quantity if self._last else 0
        qty = self._terminal_qty()
        return OrderStatusResult(filled=qty >= ordered, filled_qty=qty, avg_price=float(self.price))

    async def cancel_order(self, order_no: str) -> bool:
        self.cancel_calls.append(order_no)
        return True

    async def get_daily_executions(
        self, *, start_date: str, end_date: str, stock_code: str = ""
    ) -> list[DailyExecution]:
        return list(self.daily_execs)


class FakeNotifier:
    """Notifier 대역 — 발행된 알림을 기록."""

    def __init__(self) -> None:
        self.sent: list[object] = []

    async def emit(self, notification: object) -> str:
        self.sent.append(notification)
        return "1"


class InMemoryDcaRepo:
    """DcaRepository in-memory 대역."""

    def __init__(self) -> None:
        self.campaigns: dict[str, Campaign] = {}
        self.slices: list[SliceExecution] = []
        self._slice_id = 0

    # ----- 캠페인 -----

    async def insert_campaigns(self, rows: list[dict[str, Any]]) -> int:
        inserted = 0
        for r in rows:
            if r["id"] in self.campaigns:
                continue
            self.campaigns[r["id"]] = Campaign(
                id=r["id"],
                ticker=r["ticker"],
                status="active",
                preset=r["preset"],
                cap_krw=r["cap_krw"],
                total_slices=r["total_slices"],
                execute_at_kst=r["execute_at_kst"],
                accel_open_pct=r["accel_open_pct"],
                accel_prev_pct=r["accel_prev_pct"],
                accel_cooldown_h=r["accel_cooldown_h"],
                clamp_pct=r["clamp_pct"],
                per_order_cost_cap=r["per_order_cost_cap"],
                per_day_cost_cap=r["per_day_cost_cap"],
                dry_run=r["dry_run"],
                cumulative_filled_krw=0,
                cumulative_filled_qty=0,
                slices_done=0,
                last_accel_at=None,
                final_tail_done=False,
            )
            inserted += 1
        return inserted

    async def fetch_active_campaigns(self) -> list[Campaign]:
        return [c for c in self.campaigns.values() if c.status == "active"]

    async def fetch_campaigns(self, statuses: list[str] | None = None) -> list[Campaign]:
        cs = list(self.campaigns.values())
        if statuses:
            cs = [c for c in cs if c.status in statuses]
        return cs

    async def fetch_campaign(self, campaign_id: str) -> Campaign | None:
        return self.campaigns.get(campaign_id)

    async def count_active_for_ticker(self, ticker: str) -> int:
        return sum(
            1 for c in self.campaigns.values() if c.ticker == ticker and c.status == "active"
        )

    async def set_status(self, campaign_id: str, status: str) -> None:
        self.campaigns[campaign_id].status = status

    # ----- 슬라이스 조회 -----

    async def today_settled(self, campaign_id: str, trade_date: date) -> bool:
        return any(
            s.campaign_id == campaign_id and s.trade_date == trade_date and s.status != "pending"
            for s in self.slices
        )

    async def today_filled_krw(self, campaign_id: str, trade_date: date) -> int:
        return sum(
            s.filled_krw
            for s in self.slices
            if s.campaign_id == campaign_id and s.trade_date == trade_date
        )

    async def fetch_due_retry(self, campaign_id: str, now: datetime) -> SliceExecution | None:
        due = [
            s
            for s in self.slices
            if s.campaign_id == campaign_id
            and s.status == "rejected_retry"
            and s.retry_at is not None
            and s.retry_at <= now
        ]
        due.sort(key=lambda s: s.retry_at)  # type: ignore[arg-type,return-value]
        return due[0] if due else None

    async def fetch_submitted(self, campaign_id: str) -> list[SliceExecution]:
        return [s for s in self.slices if s.campaign_id == campaign_id and s.status == "submitted"]

    async def delete_stale_pending(self, campaign_id: str) -> int:
        before = len(self.slices)
        self.slices = [
            s for s in self.slices if not (s.campaign_id == campaign_id and s.status == "pending")
        ]
        return before - len(self.slices)

    async def recorded_order_nos(self, campaign_id: str, trade_date: date) -> set[str]:
        return {
            s.order_no
            for s in self.slices
            if s.campaign_id == campaign_id and s.trade_date == trade_date and s.order_no
        }

    # ----- 슬라이스 쓰기 -----

    async def acquire_slice(
        self,
        *,
        slice_key: str,
        campaign_id: str,
        ticker: str,
        slice_no: int,
        trigger_kind: str,
        trade_date: date,
        budget_krw: int,
    ) -> SliceExecution | None:
        if any(s.slice_key == slice_key for s in self.slices):
            return None
        self._slice_id += 1
        row = SliceExecution(
            id=self._slice_id,
            slice_key=slice_key,
            campaign_id=campaign_id,
            ticker=ticker,
            slice_no=slice_no,
            trigger_kind=trigger_kind,
            trade_date=trade_date,
            status="pending",
            budget_krw=budget_krw,
            intended_qty=None,
            order_no=None,
            filled_qty=0,
            filled_krw=0,
            avg_price=0.0,
            retry_at=None,
            retry_count=0,
            reject_reason=None,
        )
        self.slices.append(row)
        return row

    def _get(self, slice_id: int) -> SliceExecution:
        return next(s for s in self.slices if s.id == slice_id)

    async def update_slice(self, slice_id: int, **fields: Any) -> None:
        row = self._get(slice_id)
        for k, v in fields.items():
            setattr(row, k, v)

    async def finalize_slice(
        self,
        *,
        slice_id: int,
        campaign_id: str,
        status: str,
        filled_qty: int,
        filled_krw: int,
        avg_price: float,
        advance_slices_done: bool,
        set_last_accel: datetime | None = None,
        set_final_tail_done: bool = False,
        new_campaign_status: str | None = None,
    ) -> None:
        row = self._get(slice_id)
        row.status = status
        row.filled_qty = filled_qty
        row.filled_krw = filled_krw
        row.avg_price = avg_price
        camp = self.campaigns[campaign_id]
        camp.cumulative_filled_krw += filled_krw
        camp.cumulative_filled_qty += filled_qty
        if advance_slices_done:
            camp.slices_done += 1
        if set_last_accel is not None:
            camp.last_accel_at = set_last_accel
        if set_final_tail_done:
            camp.final_tail_done = True
        if new_campaign_status is not None:
            camp.status = new_campaign_status
