"""PositionSheetConsumer + TickLoop 통합 단위 테스트."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from prime_jennie_runtime.fast_loop.consumer import PositionSheetConsumer
from prime_jennie_runtime.fast_loop.entry_executor import EntryExecutor
from prime_jennie_runtime.fast_loop.exit_executor import ExitExecutor
from prime_jennie_runtime.fast_loop.notifier import Notifier
from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker
from prime_jennie_runtime.fast_loop.tick_loop import TickLoop
from prime_jennie_runtime.infra.redis_streams import (
    STREAM_POSITION_SHEETS,
    TypedStreamPublisher,
)
from prime_jennie_runtime.position_sheet.schema import KST, PositionSheet

from .fakes import FakeKisClient


def _sheet(sheet_id: str = "ps_20260416_005930_a3f2", sl_pct: float = 0.05) -> PositionSheet:
    now = datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST)
    return PositionSheet(
        sheet_id=sheet_id,
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
        entry={
            "trigger": "limit",
            "price": 70000,
            "valid_until": now + timedelta(hours=1),
        },
        exit={
            "rules": [
                {"type": "fixed_sl", "pct": sl_pct},
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


# =====================================================================
# Position Sheet Consumer
# =====================================================================


@pytest.mark.asyncio
async def test_sheet_consumer_triggers_entry_and_subscribe(fake_redis):
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient(fill_price=70000.0)
    entry_exec = EntryExecutor(kis, tracker, notifier)

    async def sizer(sheet: PositionSheet) -> int:
        return 10

    publisher = TypedStreamPublisher(fake_redis, STREAM_POSITION_SHEETS, PositionSheet)
    sheet = _sheet()
    await publisher.publish(sheet)

    consumer = PositionSheetConsumer(
        fake_redis, entry_exec, sizer, kis, group="test_fl", consumer="c1"
    )
    await consumer.ensure_group()

    # 한 번만 처리하기 위해 internal handler 직접 호출
    await consumer._handle(sheet)

    assert len(kis.buy_calls) == 1
    assert tracker.get(sheet.sheet_id) is not None
    # subscribe 호출
    assert kis.subscribe_calls == [["005930"]]


@pytest.mark.asyncio
async def test_sheet_consumer_zero_qty_skips(fake_redis):
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient()
    entry_exec = EntryExecutor(kis, tracker, notifier)

    async def sizer(sheet: PositionSheet) -> int:
        return 0

    consumer = PositionSheetConsumer(fake_redis, entry_exec, sizer, kis)
    await consumer._handle(_sheet())
    assert kis.buy_calls == []


# =====================================================================
# Tick Loop
# =====================================================================


@pytest.mark.asyncio
async def test_tick_loop_triggers_exit_on_stop_loss(fake_redis):
    """fixed_sl 5% — 진입 70000, 현재 66000 (-5.71%) → exit 트리거."""
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient(fill_price=66000.0)
    exit_exec = ExitExecutor(kis, tracker, notifier)

    sheet = _sheet(sl_pct=0.05)

    # 수동으로 entry 후 상태 (consumer 건너뛰고 직접 등록)
    from prime_jennie_runtime.fast_loop.domain import PositionState

    state = PositionState(
        sheet_id=sheet.sheet_id,
        ticker="005930",
        entry_price=70000.0,
        quantity=10,
        entered_at=datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST),
        high_watermark=70000.0,
    )
    await tracker.register(state)

    async def fetch(sheet_id: str):
        return [sheet] if sheet_id == sheet.sheet_id else []

    loop = TickLoop(fake_redis, tracker, exit_exec, fetch, group="tick_g1", consumer="tc1")
    await loop.ensure_group()

    # STREAM_PRICES에 tick 발행
    await fake_redis.xadd(
        "kis:prices",
        {
            "payload": json.dumps(
                {
                    "ticker": "005930",
                    "price": 66000.0,
                    "ts": "2026-04-16T09:30:00+09:00",
                }
            )
        },
    )

    # 한 번 읽어서 처리
    messages = await fake_redis.xreadgroup(
        "tick_g1", "tc1", {"kis:prices": ">"}, count=1, block=100
    )
    for _stream, entries in messages:
        for msg_id, data in entries:
            await loop.process_one(msg_id, data)

    # exit 실행됨 → tracker에서 제거
    assert tracker.get(state.sheet_id) is None
    assert len(kis.sell_calls) == 1


@pytest.mark.asyncio
async def test_tick_loop_no_match_persists_state(fake_redis):
    """가격 변동이 rule 트리거하지 않으면 state만 갱신 (high_watermark)."""
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient()
    exit_exec = ExitExecutor(kis, tracker, notifier)

    sheet = _sheet(sl_pct=0.10)

    from prime_jennie_runtime.fast_loop.domain import PositionState

    state = PositionState(
        sheet_id=sheet.sheet_id,
        ticker="005930",
        entry_price=70000.0,
        quantity=10,
        entered_at=datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST),
        high_watermark=70000.0,
    )
    await tracker.register(state)

    async def fetch(sheet_id: str):
        return [sheet]

    loop = TickLoop(fake_redis, tracker, exit_exec, fetch, group="tg2", consumer="tc2")
    await loop.ensure_group()
    await fake_redis.xadd(
        "kis:prices",
        {"payload": json.dumps({"ticker": "005930", "price": 71000.0})},
    )
    messages = await fake_redis.xreadgroup("tg2", "tc2", {"kis:prices": ">"}, count=1, block=100)
    for _s, entries in messages:
        for msg_id, data in entries:
            await loop.process_one(msg_id, data)

    # 여전히 active
    updated = tracker.get(state.sheet_id)
    assert updated is not None
    assert updated.high_watermark == 71000.0
    assert kis.sell_calls == []
