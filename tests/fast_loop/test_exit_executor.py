"""Exit Executor 단위 테스트."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from prime_jennie_runtime.fast_loop.domain import ExitDecision, PositionState
from prime_jennie_runtime.fast_loop.exit_executor import ExitExecutor
from prime_jennie_runtime.fast_loop.notifier import Notifier
from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker
from prime_jennie_runtime.infra.redis_streams import STREAM_NOTIFICATIONS

from .fakes import FakeKisClient


def _state() -> PositionState:
    return PositionState(
        sheet_id="ps_20260416_005930_a3f2",
        ticker="005930",
        entry_price=70000.0,
        quantity=10,
        entered_at=datetime.now(UTC),
        high_watermark=70000.0,
    )


@pytest.mark.asyncio
async def test_exit_full_close(fake_redis):
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient(fill_price=66500.0)
    executor = ExitExecutor(kis, tracker, notifier)

    state = _state()
    await tracker.register(state)

    decision = ExitDecision(should_close=True, reason="fixed_sl", portion=1.0)
    outcome = await executor.execute(state, decision)

    assert outcome.success is True
    assert outcome.fully_closed is True
    assert outcome.closed_qty == 10
    # tracker에서 제거됨
    assert tracker.get(state.sheet_id) is None
    # 시장가 sell
    assert kis.sell_calls[0].order_type == "market"
    # notification
    assert await fake_redis.xlen(STREAM_NOTIFICATIONS) == 1


@pytest.mark.asyncio
async def test_exit_scale_out_partial(fake_redis):
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient(fill_price=72500.0)
    executor = ExitExecutor(kis, tracker, notifier)

    state = _state()
    await tracker.register(state)

    decision = ExitDecision(should_close=True, reason="scale_out", portion=0.25)
    # 10 * 0.25 = 2.5 → min(2, 10)=2 예상 (int 변환)
    kis.fill_qty_override = {"SELL000001": 2}
    outcome = await executor.execute(state, decision)

    assert outcome.success is True
    assert outcome.fully_closed is False
    assert outcome.closed_qty == 2
    # 남은 수량 상태 유지
    remaining_state = tracker.get(state.sheet_id)
    assert remaining_state is not None
    assert remaining_state.quantity == 8


@pytest.mark.asyncio
async def test_breakeven_sl_raise_no_order(fake_redis):
    """should_close=False + new_sl_price → sell 주문 안 함, state.breakeven_sl_price 갱신."""
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient()
    executor = ExitExecutor(kis, tracker, notifier)

    state = _state()
    await tracker.register(state)

    decision = ExitDecision(
        should_close=False,
        reason="breakeven_sl_raise",
        new_sl_price=70210.0,
    )
    outcome = await executor.execute(state, decision)

    assert outcome.success is True
    assert outcome.closed_qty == 0
    assert kis.sell_calls == []
    # state 갱신
    restored = tracker.get(state.sheet_id)
    assert restored.breakeven_sl_price == 70210.0
    # notification 없음
    assert await fake_redis.xlen(STREAM_NOTIFICATIONS) == 0


@pytest.mark.asyncio
async def test_exit_sell_not_filled(fake_redis):
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient(should_fill=False)
    executor = ExitExecutor(kis, tracker, notifier)

    state = _state()
    await tracker.register(state)
    decision = ExitDecision(should_close=True, reason="fixed_sl", portion=1.0)
    outcome = await executor.execute(state, decision)

    assert outcome.success is False
    # tracker 유지 (아직 미청산)
    assert tracker.get(state.sheet_id) is not None
