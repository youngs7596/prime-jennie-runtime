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


@pytest.mark.asyncio
async def test_entry_dryrun_skips_kis(fake_redis):
    """DRY_RUN 키 ON 이면 KIS buy 호출 없이 시뮬레이션 outcome + notification."""
    from prime_jennie_runtime.control.state import SystemState

    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient(fill_price=70000.0)
    system_state = SystemState(fake_redis)
    executor = EntryExecutor(kis, tracker, notifier, system_state=system_state)

    await fake_redis.set("control.state:dryrun", b"1")

    sheet = _sheet(trigger="market", price=None)
    outcome = await executor.execute(sheet, quantity=10)

    assert outcome.success is True
    assert outcome.reason == "dryrun"
    assert outcome.filled_qty == 10
    assert kis.buy_calls == []  # KIS 호출 0
    assert await fake_redis.xlen(STREAM_NOTIFICATIONS) == 1


@pytest.mark.asyncio
async def test_entry_partial_fill_cancels_remainder(fake_redis):
    """confirm_order 가 부분 체결을 반환하면 잔량을 취소하고 실체결량으로 등록.

    회귀 가드 — 2026-05-21 진단: confirm 윈도우 만료 시 부분 체결을 그대로
    기록하고 잔량 주문을 남겨두면, 잔량이 v3 추적 밖에서 체결돼 보유수량이
    어긋난다 (5-14 241560 113주 중 72주만 기록 → 41주 orphan).
    """
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient(fill_price=70000.0)
    # confirm 시점 7/10 부분 체결, 취소 후 재조회도 7 (잔량 미체결)
    kis.confirm_qty_override["BUY000001"] = 7
    kis.fill_qty_override["BUY000001"] = 7
    executor = EntryExecutor(kis, tracker, notifier)

    outcome = await executor.execute(_sheet(trigger="market", price=None), quantity=10)

    assert outcome.success is True
    assert outcome.filled_qty == 7  # 주문량 10 이 아닌 실체결량 7 로 확정
    assert "BUY000001" in kis.cancel_calls  # 미체결 잔량 취소
    state = tracker.get(outcome.sheet_id)
    assert state is not None
    assert state.quantity == 7  # v3 보유수량 = KIS 실체결량


@pytest.mark.asyncio
async def test_entry_partial_then_full_on_recheck(fake_redis):
    """confirm 시점엔 부분 체결이나 취소 직후 재조회에서 전량 체결로 확정되는 경우."""
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient(fill_price=70000.0)
    kis.confirm_qty_override["BUY000001"] = 7  # confirm 시점 7/10
    kis.fill_qty_override["BUY000001"] = 10  # 취소 처리 중 잔량 체결 → terminal 10
    executor = EntryExecutor(kis, tracker, notifier)

    outcome = await executor.execute(_sheet(trigger="market", price=None), quantity=10)

    assert outcome.success is True
    assert outcome.filled_qty == 10  # terminal 재조회값으로 확정
    assert "BUY000001" in kis.cancel_calls
    state = tracker.get(outcome.sheet_id)
    assert state is not None
    assert state.quantity == 10
