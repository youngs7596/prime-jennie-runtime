"""PositionSheetConsumer + TickLoop 통합 단위 테스트.

POSITION_SHEET_SPEC §4.1 / §4.3 의 흐름 (시트 → 큐 적재 → tick 마다 conditions
재평가 → 만족 시 entry) 을 검증.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from prime_jennie_runtime.fast_loop.consumer import PositionSheetConsumer
from prime_jennie_runtime.fast_loop.entry_executor import EntryExecutor
from prime_jennie_runtime.fast_loop.exit_executor import ExitExecutor
from prime_jennie_runtime.fast_loop.notifier import Notifier
from prime_jennie_runtime.fast_loop.pending_entry import (
    EntryConditionEvaluator,
    PendingEntryQueue,
)
from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker
from prime_jennie_runtime.fast_loop.tick_loop import TickLoop
from prime_jennie_runtime.position_sheet.schema import KST, PositionSheet

from .fakes import FakeKisClient


def _sheet(
    sheet_id: str = "ps_20260416_005930_a3f2",
    sl_pct: float = 0.05,
    *,
    ticker: str = "005930",
    conditions: list[dict] | None = None,
) -> PositionSheet:
    now = datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST)
    return PositionSheet(
        sheet_id=sheet_id,
        generated_at=now,
        valid_until=now + timedelta(hours=6),
        ticker=ticker,
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
            "conditions": conditions or [],
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
# Position Sheet Consumer — 시트 → 큐 적재
# =====================================================================


@pytest.mark.asyncio
async def test_sheet_consumer_enqueues_and_subscribes(fake_redis):
    """시트 도착 시 즉시 매수 X, pending queue 적재 + subscribe."""
    tracker = PositionTracker(fake_redis)
    queue = PendingEntryQueue(fake_redis)
    kis = FakeKisClient(fill_price=70000.0)

    consumer = PositionSheetConsumer(fake_redis, queue, tracker, kis)

    sheet = _sheet()
    await consumer._handle(sheet)

    # 즉시 매수 안 함
    assert kis.buy_calls == []
    # 큐에 적재됨
    assert queue.contains_sheet(sheet.sheet_id) is True
    # subscribe 호출
    assert kis.subscribe_calls == [["005930"]]


@pytest.mark.asyncio
async def test_sheet_consumer_dedups_when_holding(fake_redis):
    """이미 보유 중인 ticker 의 시트는 거부."""
    tracker = PositionTracker(fake_redis)
    queue = PendingEntryQueue(fake_redis)
    kis = FakeKisClient()

    # 보유 중 상태 등록
    from prime_jennie_runtime.fast_loop.domain import PositionState

    state = PositionState(
        sheet_id="existing_sheet",
        ticker="005930",
        entry_price=70000.0,
        quantity=10,
        entered_at=datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST),
    )
    await tracker.register(state)

    consumer = PositionSheetConsumer(fake_redis, queue, tracker, kis)
    await consumer._handle(_sheet())

    assert queue.size() == 0
    assert kis.subscribe_calls == []


@pytest.mark.asyncio
async def test_sheet_consumer_dedups_when_ticker_already_pending(fake_redis):
    """같은 ticker 의 다른 시트가 이미 큐에 있으면 두 번째 시트 거부."""
    tracker = PositionTracker(fake_redis)
    queue = PendingEntryQueue(fake_redis)
    kis = FakeKisClient()

    consumer = PositionSheetConsumer(fake_redis, queue, tracker, kis)

    s1 = _sheet("ps_20260416_005930_a3f2")
    s2 = _sheet("ps_20260416_005930_b1c3")  # 같은 ticker, 다른 sheet_id
    await consumer._handle(s1)
    await consumer._handle(s2)

    assert queue.size() == 1
    # 첫 번째 시트만 적재
    assert queue.contains_sheet(s1.sheet_id) is True
    assert queue.contains_sheet(s2.sheet_id) is False
    # subscribe 도 첫 번째만
    assert kis.subscribe_calls == [["005930"]]


# =====================================================================
# Tick Loop — exit path
# =====================================================================


def _build_tick_loop(fake_redis, *, exit_executor, sheet_fetcher, **overrides):
    """TickLoop 시그니처가 길어 헬퍼로 분리. 기본 fake 큐/evaluator 주입."""
    queue = overrides.pop("pending_queue", PendingEntryQueue(fake_redis))
    evaluator = overrides.pop("entry_evaluator", EntryConditionEvaluator())
    entry_exec = overrides.pop("entry_executor", None)
    sizer = overrides.pop(
        "account_sizer",
        _make_zero_sizer(),
    )
    return TickLoop(
        fake_redis,
        overrides.pop("tracker"),
        exit_executor,
        sheet_fetcher,
        queue,
        evaluator,
        entry_exec,
        sizer,
        **overrides,
    )


def _frozen_clock(when: datetime):
    """장 개장 후 시각으로 고정된 clock — entry path 테스트용."""

    def clock():
        return when

    return clock


def _make_zero_sizer():
    async def sizer(_sheet):
        return 0

    return sizer


@pytest.mark.asyncio
async def test_tick_loop_triggers_exit_on_stop_loss(fake_redis):
    """fixed_sl 5% — 진입 70000, 현재 66000 (-5.71%) → exit 트리거."""
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient(fill_price=66000.0)
    exit_exec = ExitExecutor(kis, tracker, notifier)
    entry_exec = EntryExecutor(kis, tracker, notifier)

    sheet = _sheet(sl_pct=0.05)

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

    loop = _build_tick_loop(
        fake_redis,
        tracker=tracker,
        exit_executor=exit_exec,
        sheet_fetcher=fetch,
        entry_executor=entry_exec,
        group="tick_g1",
        consumer="tc1",
    )
    await loop.ensure_group()

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
    messages = await fake_redis.xreadgroup(
        "tick_g1", "tc1", {"kis:prices": ">"}, count=1, block=100
    )
    for _stream, entries in messages:
        for msg_id, data in entries:
            await loop.process_one(msg_id, data)

    assert tracker.get(state.sheet_id) is None
    assert len(kis.sell_calls) == 1


@pytest.mark.asyncio
async def test_tick_loop_no_match_persists_state(fake_redis):
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient()
    exit_exec = ExitExecutor(kis, tracker, notifier)
    entry_exec = EntryExecutor(kis, tracker, notifier)

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

    loop = _build_tick_loop(
        fake_redis,
        tracker=tracker,
        exit_executor=exit_exec,
        sheet_fetcher=fetch,
        entry_executor=entry_exec,
        group="tg2",
        consumer="tc2",
    )
    await loop.ensure_group()

    tick_ts = datetime(2026, 4, 16, 10, 0, 0, tzinfo=KST).isoformat()
    await fake_redis.xadd(
        "kis:prices",
        {"payload": json.dumps({"ticker": "005930", "price": 71000.0, "ts": tick_ts})},
    )
    messages = await fake_redis.xreadgroup("tg2", "tc2", {"kis:prices": ">"}, count=1, block=100)
    for _s, entries in messages:
        for msg_id, data in entries:
            await loop.process_one(msg_id, data)

    updated = tracker.get(state.sheet_id)
    assert updated is not None
    assert updated.high_watermark == 71000.0
    assert kis.sell_calls == []


# =====================================================================
# Tick Loop — entry path (pending queue 재평가)
# =====================================================================


@pytest.mark.asyncio
async def test_tick_loop_entry_fires_when_conditions_empty(fake_redis):
    """conditions=[] 시트는 첫 tick 에 즉시 entry."""
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient(fill_price=70000.0)
    entry_exec = EntryExecutor(kis, tracker, notifier)
    exit_exec = ExitExecutor(kis, tracker, notifier)
    queue = PendingEntryQueue(fake_redis)

    sheet = _sheet(conditions=[])
    await queue.add(sheet)

    async def fetch(sheet_id: str):
        return [sheet]

    async def sizer(_s):
        return 5

    frozen = datetime(2026, 4, 16, 9, 30, tzinfo=KST)
    loop = _build_tick_loop(
        fake_redis,
        tracker=tracker,
        exit_executor=exit_exec,
        sheet_fetcher=fetch,
        pending_queue=queue,
        entry_executor=entry_exec,
        account_sizer=sizer,
        clock=_frozen_clock(frozen),
        group="tg_e1",
        consumer="tce1",
    )
    await loop.ensure_group()

    await fake_redis.xadd(
        "kis:prices",
        {
            "payload": json.dumps(
                {
                    "ticker": "005930",
                    "price": 70000.0,
                    "ts": frozen.isoformat(),
                }
            )
        },
    )
    messages = await fake_redis.xreadgroup("tg_e1", "tce1", {"kis:prices": ">"}, count=1, block=100)
    for _s, entries in messages:
        for msg_id, data in entries:
            await loop.process_one(msg_id, data)

    assert len(kis.buy_calls) == 1
    assert tracker.get(sheet.sheet_id) is not None
    assert queue.contains_sheet(sheet.sheet_id) is False


@pytest.mark.asyncio
async def test_tick_loop_entry_blocked_when_price_below_fails(fake_redis):
    """price_below 조건 미만족 시 entry 보류, 큐에 남음."""
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient(fill_price=70000.0)
    entry_exec = EntryExecutor(kis, tracker, notifier)
    exit_exec = ExitExecutor(kis, tracker, notifier)
    queue = PendingEntryQueue(fake_redis)

    # 70500 미만일 때만 진입 — tick 71000 이면 fail
    sheet = _sheet(conditions=[{"type": "price_below", "value": 70500}])
    await queue.add(sheet)

    async def fetch(sheet_id: str):
        return [sheet]

    async def sizer(_s):
        return 5

    frozen = datetime(2026, 4, 16, 9, 30, tzinfo=KST)
    loop = _build_tick_loop(
        fake_redis,
        tracker=tracker,
        exit_executor=exit_exec,
        sheet_fetcher=fetch,
        pending_queue=queue,
        entry_executor=entry_exec,
        account_sizer=sizer,
        clock=_frozen_clock(frozen),
        group="tg_e2",
        consumer="tce2",
    )
    await loop.ensure_group()

    await fake_redis.xadd(
        "kis:prices",
        {
            "payload": json.dumps(
                {
                    "ticker": "005930",
                    "price": 71000.0,  # 조건 fail
                    "ts": datetime(2026, 4, 16, 9, 30, tzinfo=KST).isoformat(),
                }
            )
        },
    )
    messages = await fake_redis.xreadgroup("tg_e2", "tce2", {"kis:prices": ">"}, count=1, block=100)
    for _s, entries in messages:
        for msg_id, data in entries:
            await loop.process_one(msg_id, data)

    # 매수 안 함, 큐에 남음
    assert kis.buy_calls == []
    assert queue.contains_sheet(sheet.sheet_id) is True
