"""manual_sell / manual_sellall 의 v3 state 정합 path.

design: 2026-05-15 audit D1 의 manual_sell silent gap fix.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import fakeredis.aioredis
import pytest
from pydantic import BaseModel

from prime_jennie_runtime.control.consumer import ControlCommandConsumer
from prime_jennie_runtime.fast_loop.domain import PositionState
from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker
from prime_jennie_runtime.kis_gateway.schemas import (
    OrderResult,
    OrderStatusResult,
    PortfolioState,
)
from prime_jennie_runtime.kis_gateway.schemas import (
    Position as KisPosition,
)
from prime_jennie_runtime.position_sheet.schema import KST
from prime_jennie_runtime.telegram_bot.control import ControlCommand
from tests.fast_loop.test_consumer_and_tick import _sheet

# ─── fakes ─────────────────────────────────────────────────────────────


class _FakeKis:
    def __init__(
        self,
        *,
        sell_success: bool = True,
        confirm: OrderStatusResult | None = None,
        balance_positions: list[KisPosition] | None = None,
    ) -> None:
        self.sell_calls: list[tuple[str, int]] = []
        self.confirm_calls: list[str] = []
        self._sell_success = sell_success
        self._confirm = confirm or OrderStatusResult(filled=True, filled_qty=10, avg_price=72000.0)
        self._balance_positions = balance_positions or []
        self._counter = 0

    async def sell(self, order):
        self._counter += 1
        self.sell_calls.append((order.stock_code, order.quantity))
        return OrderResult(
            success=self._sell_success,
            order_no=f"SELL{self._counter:06d}" if self._sell_success else None,
            stock_code=order.stock_code,
            quantity=order.quantity,
            price=0,
            message=None if self._sell_success else "rejected",
        )

    async def confirm_order(self, order_no, *, max_retries=5, interval=2.0):
        self.confirm_calls.append(order_no)
        return self._confirm

    async def get_balance(self):
        return PortfolioState(
            cash_balance=10_000_000,
            total_asset=20_000_000,
            stock_eval_amount=10_000_000,
            positions=self._balance_positions,
            position_count=len(self._balance_positions),
            timestamp=datetime.now(UTC),
        )


class _FakeRecorder:
    def __init__(self) -> None:
        self.sells: list[dict] = []
        self.buys: list[dict] = []

    async def record_sell(self, sheet, *, filled_qty, filled_price, executed_at, reason):
        self.sells.append(
            {
                "sheet_id": sheet.sheet_id,
                "filled_qty": filled_qty,
                "filled_price": filled_price,
                "reason": reason,
            }
        )

    async def record_buy(self, sheet, *, filled_qty, filled_price, executed_at):
        self.buys.append({"sheet_id": sheet.sheet_id, "filled_qty": filled_qty})


class _FakeNotifier:
    def __init__(self) -> None:
        self.emitted: list[BaseModel] = []

    async def emit(self, n):
        self.emitted.append(n)
        return "stub"

    async def close(self):
        pass


def _fetch_sheet_factory(sheet):
    async def _fetch(sheet_id):
        return [sheet] if sheet.sheet_id == sheet_id else []

    return _fetch


# ─── helpers ──────────────────────────────────────────────────────────


async def _setup_consumer(
    redis,
    kis,
    *,
    tracker=None,
    recorder=None,
    sheet_fetcher=None,
    notifier=None,
):
    return ControlCommandConsumer(
        redis,
        consumer_name="test-manual",
        kis_client=kis,
        tracker=tracker,
        recorder=recorder,
        sheet_fetcher=sheet_fetcher,
        notifier=notifier,
        max_confirm_retries=1,
        confirm_interval=0.01,
    )


def _cmd(kind: str, ticker: str = "005930", qty: int = 10) -> ControlCommand:
    return ControlCommand(
        kind=kind,
        issued_by="test",
        reason="test",
        payload={"ticker": ticker, "quantity": qty},
        issued_at=datetime.now(UTC),
    )


# ─── tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_manual_sell_full_path_when_sheet_exists():
    """sheet 있는 종목 → tracker close + record_sell + notifier + publish."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        tracker = PositionTracker(redis)
        sheet = _sheet("ps_20260514_005930_aaaa", ticker="005930")
        state = PositionState(
            sheet_id=sheet.sheet_id,
            ticker="005930",
            entry_price=70_000.0,
            quantity=10,
            entered_at=datetime(2026, 5, 14, 9, 30, tzinfo=KST),
        )
        await tracker.register(state)

        kis = _FakeKis(confirm=OrderStatusResult(filled=True, filled_qty=10, avg_price=72_000.0))
        recorder = _FakeRecorder()
        notifier = _FakeNotifier()
        consumer = await _setup_consumer(
            redis,
            kis,
            tracker=tracker,
            recorder=recorder,
            sheet_fetcher=_fetch_sheet_factory(sheet),
            notifier=notifier,
        )

        await consumer.apply(_cmd("manual_sell", "005930", 10))

        # KIS 호출됨
        assert kis.sell_calls == [("005930", 10)]
        # 체결 확인 호출됨
        assert len(kis.confirm_calls) == 1
        # tracker — 전량 close
        assert tracker.sheet_ids_for("005930") == []
        # executions INSERT 호출됨
        assert len(recorder.sells) == 1
        assert recorder.sells[0]["sheet_id"] == sheet.sheet_id
        assert recorder.sells[0]["reason"] == "manual_telegram"
        # notifier emit (TradeNotification)
        assert len(notifier.emitted) == 1
        n = notifier.emitted[0]
        assert n.kind == "exit_filled"
        assert n.side == "sell"
        assert n.pnl_amount == (72_000.0 - 70_000.0) * 10
        # ExitFilledEvent publish
        msgs = await redis.xrange("coordinator:events")
        assert len(msgs) == 1
        _, fields = msgs[0]
        payload = json.loads(fields[b"payload"])
        assert payload["event_kind"] == "exit_filled"
        assert payload["reason"] == "manual_telegram"
        assert payload["fully_closed"] is True
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_manual_sell_partial_keeps_tracker_state_with_reduced_qty():
    """부분 매도 → tracker.state.quantity 감소 + persist."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        tracker = PositionTracker(redis)
        sheet = _sheet("ps_20260514_005930_aaaa", ticker="005930")
        state = PositionState(
            sheet_id=sheet.sheet_id,
            ticker="005930",
            entry_price=70_000.0,
            quantity=20,
            entered_at=datetime(2026, 5, 14, 9, 30, tzinfo=KST),
        )
        await tracker.register(state)

        kis = _FakeKis(confirm=OrderStatusResult(filled=True, filled_qty=8, avg_price=71_000.0))
        recorder = _FakeRecorder()
        notifier = _FakeNotifier()
        consumer = await _setup_consumer(
            redis,
            kis,
            tracker=tracker,
            recorder=recorder,
            sheet_fetcher=_fetch_sheet_factory(sheet),
            notifier=notifier,
        )

        await consumer.apply(_cmd("manual_sell", "005930", 8))

        # tracker — 아직 보유 12주
        assert tracker.sheet_ids_for("005930") == [sheet.sheet_id]
        remaining = tracker._states[sheet.sheet_id]  # noqa: SLF001
        assert remaining.quantity == 12
        # is_partial=True 인 notification
        assert notifier.emitted[0].is_partial is True
        # publish_event 의 fully_closed=False
        msgs = await redis.xrange("coordinator:events")
        payload = json.loads(msgs[0][1][b"payload"])
        assert payload["fully_closed"] is False
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_manual_sell_skips_finalize_when_no_sheet():
    """sheet 없는 외부 보유 종목 → KIS 만 호출, v3 state 미변경."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        tracker = PositionTracker(redis)
        kis = _FakeKis()
        recorder = _FakeRecorder()
        notifier = _FakeNotifier()
        consumer = await _setup_consumer(
            redis,
            kis,
            tracker=tracker,
            recorder=recorder,
            sheet_fetcher=_fetch_sheet_factory(_sheet()),
            notifier=notifier,
        )

        await consumer.apply(_cmd("manual_sell", "999999", 5))

        # KIS 는 호출됨
        assert kis.sell_calls == [("999999", 5)]
        # v3 state — 변경 없음
        assert recorder.sells == []
        assert notifier.emitted == []
        msgs = await redis.xrange("coordinator:events")
        assert msgs == []
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_manual_sell_skips_finalize_when_deps_missing():
    """tracker/recorder/sheet_fetcher/notifier 미주입 → legacy 모드 (KIS 만)."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        kis = _FakeKis()
        consumer = await _setup_consumer(redis, kis)  # deps 미주입
        await consumer.apply(_cmd("manual_sell", "005930", 10))
        assert kis.sell_calls == [("005930", 10)]
        msgs = await redis.xrange("coordinator:events")
        assert msgs == []
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_manual_sell_kis_reject_no_finalize():
    """KIS reject → state 변경 없음."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        tracker = PositionTracker(redis)
        sheet = _sheet("ps_20260514_005930_aaaa", ticker="005930")
        state = PositionState(
            sheet_id=sheet.sheet_id,
            ticker="005930",
            entry_price=70_000.0,
            quantity=10,
            entered_at=datetime(2026, 5, 14, 9, 30, tzinfo=KST),
        )
        await tracker.register(state)

        kis = _FakeKis(sell_success=False)
        recorder = _FakeRecorder()
        notifier = _FakeNotifier()
        consumer = await _setup_consumer(
            redis,
            kis,
            tracker=tracker,
            recorder=recorder,
            sheet_fetcher=_fetch_sheet_factory(sheet),
            notifier=notifier,
        )

        await consumer.apply(_cmd("manual_sell", "005930", 10))

        # tracker 아직 보유
        assert tracker.sheet_ids_for("005930") == [sheet.sheet_id]
        # 정합 단계 모두 skip
        assert recorder.sells == []
        assert notifier.emitted == []
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_manual_sell_not_filled_no_finalize():
    """KIS 주문 발행 성공했지만 filled_qty=0 → state 변경 없음."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        tracker = PositionTracker(redis)
        sheet = _sheet("ps_20260514_005930_aaaa", ticker="005930")
        state = PositionState(
            sheet_id=sheet.sheet_id,
            ticker="005930",
            entry_price=70_000.0,
            quantity=10,
            entered_at=datetime(2026, 5, 14, 9, 30, tzinfo=KST),
        )
        await tracker.register(state)

        kis = _FakeKis(confirm=OrderStatusResult(filled=False, filled_qty=0, avg_price=0.0))
        recorder = _FakeRecorder()
        consumer = await _setup_consumer(
            redis,
            kis,
            tracker=tracker,
            recorder=recorder,
            sheet_fetcher=_fetch_sheet_factory(sheet),
            notifier=_FakeNotifier(),
        )

        await consumer.apply(_cmd("manual_sell", "005930", 10))

        assert tracker.sheet_ids_for("005930") == [sheet.sheet_id]
        assert recorder.sells == []
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_manual_sellall_finalizes_each_held_sheet():
    """sellall — 보유 종목 각각에 _execute_manual_sell 적용 + 정합."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        tracker = PositionTracker(redis)
        sheet_a = _sheet("ps_20260514_005930_aaaa", ticker="005930")
        sheet_b = _sheet("ps_20260514_000660_bbbb", ticker="000660")
        for s in (sheet_a, sheet_b):
            await tracker.register(
                PositionState(
                    sheet_id=s.sheet_id,
                    ticker=s.ticker,
                    entry_price=50_000.0,
                    quantity=10,
                    entered_at=datetime(2026, 5, 14, 9, 30, tzinfo=KST),
                )
            )

        kis = _FakeKis(
            confirm=OrderStatusResult(filled=True, filled_qty=10, avg_price=52_000.0),
            balance_positions=[
                KisPosition(
                    stock_code="005930",
                    stock_name="삼성전자",
                    quantity=10,
                    average_buy_price=50_000,
                    total_buy_amount=500_000,
                ),
                KisPosition(
                    stock_code="000660",
                    stock_name="SK하이닉스",
                    quantity=10,
                    average_buy_price=120_000,
                    total_buy_amount=1_200_000,
                ),
            ],
        )
        recorder = _FakeRecorder()
        notifier = _FakeNotifier()

        async def fetch(sheet_id):
            for s in (sheet_a, sheet_b):
                if s.sheet_id == sheet_id:
                    return [s]
            return []

        consumer = await _setup_consumer(
            redis,
            kis,
            tracker=tracker,
            recorder=recorder,
            sheet_fetcher=fetch,
            notifier=notifier,
        )

        await consumer.apply(
            ControlCommand(
                kind="manual_sellall",
                issued_by="test",
                reason="test",
                payload={},
                issued_at=datetime.now(UTC),
            )
        )

        assert {c[0] for c in kis.sell_calls} == {"005930", "000660"}
        assert len(recorder.sells) == 2
        assert tracker.tickers_active() == []
        # 2 exit_filled events publish 됨
        msgs = await redis.xrange("coordinator:events")
        assert len(msgs) == 2
    finally:
        await redis.aclose()
