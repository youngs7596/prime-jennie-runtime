"""Entry Executor 단위 테스트."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from prime_jennie_runtime.fast_loop.entry_executor import EntryExecutor, _align_tick_size
from prime_jennie_runtime.fast_loop.notifier import Notifier
from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker
from prime_jennie_runtime.infra.redis_streams import STREAM_NOTIFICATIONS
from prime_jennie_runtime.position_sheet.schema import KST, PositionSheet

from .fakes import FakeKisClient


def _sheet(trigger: str = "limit", price: int | None = 70000) -> PositionSheet:
    now = datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST)
    entry_data: dict = {"trigger": trigger, "valid_until": now + timedelta(hours=1)}
    if price is not None:
        entry_data["price"] = price
    return PositionSheet(
        sheet_id="ps_20260416_005930_a3f2",
        generated_at=now,
        valid_until=now + timedelta(hours=6),
        ticker="005930",
        strategy_tag="SECTOR_MOMENTUM",
        size={
            "base_pct": 0.05,
            "macro_multiplier": 1.0,
            "risk_multiplier": 1.0,
            "final_pct": 0.05,
            "max_notional_krw": 5_000_000,
        },
        entry=entry_data,
        exit={
            "rules": [
                {"type": "fixed_sl", "pct": 0.05},
                {"type": "time_stop", "mode": "eod"},
            ]
        },
        provenance={
            "scout_run_id": "scout_X",
            "scout_code_hash": "sha256:x",
            "scout_hypothesis": "test",
            "macro_state_snapshot": {
                "gate": "open",
                "size_multiplier": 1.0,
                "gate_run_id": "macro_X",
            },
            "strategy_policy_version": "v3.0.1",
            "generated_by": "test",
        },
    )


def test_align_tick_size():
    assert _align_tick_size(7) == 7
    assert _align_tick_size(4999) == 4995
    assert _align_tick_size(19999) == 19990
    assert _align_tick_size(70000) == 70000
    assert _align_tick_size(70123) == 70100
    assert _align_tick_size(199999) == 199900
    assert _align_tick_size(499999) == 499500
    assert _align_tick_size(1_234_567) == 1_234_000


@pytest.mark.asyncio
async def test_entry_limit_success(fake_redis):
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient(fill_price=70000.0)
    executor = EntryExecutor(kis, tracker, notifier)

    outcome = await executor.execute(_sheet(), quantity=10)

    assert outcome.success is True
    assert outcome.filled_qty == 10
    assert outcome.filled_price == 70000.0
    # tracker 등록 확인
    assert tracker.get(outcome.sheet_id) is not None
    # 주문 payload 확인 — limit + 호가 단위 정렬
    assert kis.buy_calls[0].order_type == "limit"
    assert kis.buy_calls[0].price == 70000
    # notification 발행
    length = await fake_redis.xlen(STREAM_NOTIFICATIONS)
    assert length == 1


@pytest.mark.asyncio
async def test_entry_market(fake_redis):
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient(fill_price=70000.0)
    executor = EntryExecutor(kis, tracker, notifier)

    outcome = await executor.execute(_sheet(trigger="market", price=None), quantity=10)
    assert outcome.success is True
    assert kis.buy_calls[0].order_type == "market"
    assert kis.buy_calls[0].price is None


@pytest.mark.asyncio
async def test_entry_not_filled_cancels(fake_redis):
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient(should_fill=False)
    executor = EntryExecutor(kis, tracker, notifier)

    outcome = await executor.execute(_sheet(), quantity=10)

    assert outcome.success is False
    assert outcome.reason == "not_filled"
    assert len(kis.cancel_calls) == 1
    # tracker 등록 안 됨
    assert tracker.active_sheet_ids() == []


@pytest.mark.asyncio
async def test_entry_zero_quantity(fake_redis):
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient()
    executor = EntryExecutor(kis, tracker, notifier)
    outcome = await executor.execute(_sheet(), quantity=0)
    assert outcome.success is False
    assert outcome.reason == "invalid_order_request"
    assert kis.buy_calls == []
