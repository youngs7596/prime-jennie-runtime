"""PositionTracker 단위 테스트 — 등록/조회/재시작 복구."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from prime_jennie_runtime.fast_loop.domain import PositionState
from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker


def _state(sheet_id: str = "ps_20260416_005930_a3f2", ticker: str = "005930") -> PositionState:
    return PositionState(
        sheet_id=sheet_id,
        ticker=ticker,
        entry_price=70000.0,
        quantity=10,
        entered_at=datetime(2026, 4, 16, 9, 15, 0, tzinfo=UTC),
        high_watermark=70000.0,
    )


@pytest.mark.asyncio
async def test_register_and_get(fake_redis):
    tracker = PositionTracker(fake_redis)
    await tracker.register(_state())
    got = tracker.get("ps_20260416_005930_a3f2")
    assert got is not None
    assert got.ticker == "005930"


@pytest.mark.asyncio
async def test_sheet_ids_for_ticker(fake_redis):
    tracker = PositionTracker(fake_redis)
    await tracker.register(_state(sheet_id="ps_20260416_005930_aaaa"))
    await tracker.register(_state(sheet_id="ps_20260416_005930_bbbb"))
    ids = tracker.sheet_ids_for("005930")
    assert len(ids) == 2


@pytest.mark.asyncio
async def test_close_removes_state(fake_redis):
    tracker = PositionTracker(fake_redis)
    await tracker.register(_state())
    await tracker.close("ps_20260416_005930_a3f2")
    assert tracker.get("ps_20260416_005930_a3f2") is None
    assert tracker.sheet_ids_for("005930") == []


@pytest.mark.asyncio
async def test_load_from_redis_restores(fake_redis):
    t1 = PositionTracker(fake_redis)
    state = _state()
    state.high_watermark = 72500.0
    state.peak_return_pct = 0.0357
    state.scale_out_executed.add(0)
    await t1.register(state)

    # 새 인스턴스 — 실제로는 재시작 상황
    t2 = PositionTracker(fake_redis)
    count = await t2.load_from_redis()
    assert count == 1

    restored = t2.get(state.sheet_id)
    assert restored is not None
    assert restored.high_watermark == 72500.0
    assert restored.scale_out_executed == {0}


@pytest.mark.asyncio
async def test_persist_updates_redis(fake_redis):
    tracker = PositionTracker(fake_redis)
    state = _state()
    await tracker.register(state)

    state.high_watermark = 75000.0
    await tracker.persist(state.sheet_id)

    t2 = PositionTracker(fake_redis)
    await t2.load_from_redis()
    assert t2.get(state.sheet_id).high_watermark == 75000.0


@pytest.mark.asyncio
async def test_multiple_tickers(fake_redis):
    tracker = PositionTracker(fake_redis)
    await tracker.register(_state(sheet_id="ps_20260416_005930_a", ticker="005930"))
    await tracker.register(_state(sheet_id="ps_20260416_000660_b", ticker="000660"))
    assert set(tracker.tickers_active()) == {"005930", "000660"}
