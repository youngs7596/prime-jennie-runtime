"""PositionSheetConsumer 의 recent_stoploss_cooldown 가드.

design: .ai/designs/2026-05-15-cooldown-and-duplicate-guard.md §3
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from prime_jennie_runtime.fast_loop.consumer import PositionSheetConsumer
from prime_jennie_runtime.fast_loop.cooldown_check import (
    CooldownChecker,
    NullCooldownChecker,
    RecentStopLoss,
)
from prime_jennie_runtime.fast_loop.pending_entry import PendingEntryQueue
from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker
from prime_jennie_runtime.position_sheet.schema import KST

from .fakes import FakeKisClient
from .test_consumer_and_tick import _sheet


class _StubCooldownChecker(CooldownChecker):
    """미리 정해둔 결과를 반환하는 fake."""

    def __init__(self, recent: RecentStopLoss | None) -> None:
        self.recent = recent
        self.calls: list[str] = []

    async def has_recent_stoploss(self, ticker: str) -> RecentStopLoss | None:
        self.calls.append(ticker)
        return self.recent


class _ExplodingCooldownChecker(CooldownChecker):
    """예외를 던지는 fake — fail-open 검증."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def has_recent_stoploss(self, ticker: str) -> RecentStopLoss | None:
        self.calls.append(ticker)
        raise RuntimeError("db gone")


def _now() -> datetime:
    return datetime(2026, 5, 15, 9, 30, 0, tzinfo=KST)


@pytest.mark.asyncio
async def test_null_checker_is_default_and_does_not_block(fake_redis):
    """cooldown_checker 미주입 → NullCooldownChecker fallback → enqueue 정상."""
    tracker = PositionTracker(fake_redis)
    queue = PendingEntryQueue(fake_redis)
    kis = FakeKisClient()

    consumer = PositionSheetConsumer(fake_redis, queue, tracker, kis)
    assert isinstance(consumer._cooldown, NullCooldownChecker)

    await consumer._handle(_sheet())
    assert queue.size() == 1
    assert kis.subscribe_calls == [["005930"]]


@pytest.mark.asyncio
async def test_recent_stoploss_rejects_sheet(fake_redis):
    """24h 내 손절 이력 있는 종목 → enqueue skip + entry_rejected 발행."""
    tracker = PositionTracker(fake_redis)
    queue = PendingEntryQueue(fake_redis)
    kis = FakeKisClient()

    recent = RecentStopLoss(
        ticker="005930",
        last_exit_at=_now() - timedelta(hours=2),
        reason="fixed_sl",
    )
    checker = _StubCooldownChecker(recent=recent)

    consumer = PositionSheetConsumer(fake_redis, queue, tracker, kis, cooldown_checker=checker)
    await consumer._handle(_sheet())

    assert queue.size() == 0
    assert kis.subscribe_calls == []
    assert checker.calls == ["005930"]

    # entry_rejected event 가 coordinator stream 에 publish 됨
    import json

    msgs = await fake_redis.xrange("coordinator:events")
    assert len(msgs) == 1
    _, fields = msgs[0]
    payload = json.loads(fields[b"payload"])
    assert payload["event_kind"] == "entry_rejected"
    assert payload["reason"] == "recent_stoploss_cooldown"
    assert payload["ticker"] == "005930"


@pytest.mark.asyncio
async def test_no_recent_stoploss_passes_through(fake_redis):
    """cooldown checker None 반환 → 정상 enqueue."""
    tracker = PositionTracker(fake_redis)
    queue = PendingEntryQueue(fake_redis)
    kis = FakeKisClient()
    checker = _StubCooldownChecker(recent=None)

    consumer = PositionSheetConsumer(fake_redis, queue, tracker, kis, cooldown_checker=checker)
    await consumer._handle(_sheet())

    assert queue.size() == 1
    assert checker.calls == ["005930"]


@pytest.mark.asyncio
async def test_checker_exception_is_fail_open(fake_redis):
    """checker 가 예외 → enqueue 진행 (fail-open, 매매 path 보호)."""
    tracker = PositionTracker(fake_redis)
    queue = PendingEntryQueue(fake_redis)
    kis = FakeKisClient()
    checker = _ExplodingCooldownChecker()

    consumer = PositionSheetConsumer(fake_redis, queue, tracker, kis, cooldown_checker=checker)
    await consumer._handle(_sheet())

    assert queue.size() == 1
    assert checker.calls == ["005930"]


@pytest.mark.asyncio
async def test_cooldown_not_checked_when_already_holding(fake_redis):
    """이미 보유 중이면 cooldown 검사 건너뛴다 (already_holding 분기 우선)."""
    from prime_jennie_runtime.fast_loop.domain import PositionState

    tracker = PositionTracker(fake_redis)
    queue = PendingEntryQueue(fake_redis)
    kis = FakeKisClient()
    checker = _StubCooldownChecker(recent=RecentStopLoss("005930", _now(), "fixed_sl"))

    state = PositionState(
        sheet_id="existing",
        ticker="005930",
        entry_price=70000.0,
        quantity=10,
        entered_at=_now(),
    )
    await tracker.register(state)

    consumer = PositionSheetConsumer(fake_redis, queue, tracker, kis, cooldown_checker=checker)
    await consumer._handle(_sheet())

    assert queue.size() == 0
    assert checker.calls == []  # already_holding 분기에서 return — cooldown 미호출


@pytest.mark.asyncio
async def test_cooldown_not_checked_when_ticker_pending(fake_redis):
    """같은 ticker 가 이미 큐에 있으면 cooldown 검사 건너뛴다."""
    tracker = PositionTracker(fake_redis)
    queue = PendingEntryQueue(fake_redis)
    kis = FakeKisClient()
    checker = _StubCooldownChecker(recent=RecentStopLoss("005930", _now(), "fixed_sl"))

    consumer = PositionSheetConsumer(fake_redis, queue, tracker, kis, cooldown_checker=checker)

    s1 = _sheet("ps_20260515_005930_aaaa")
    s2 = _sheet("ps_20260515_005930_bbbb")
    # s1 은 첫 통과 (cooldown=None 시점) — checker 가 stub 라 항상 RecentStopLoss
    # 반환이므로 사실 s1 도 reject 됨. ticker_pending 시나리오만 검증하려면
    # s1 만 정상 enqueue 시켜야 — 다른 ticker 로 1개 미리 적재 후 같은 ticker
    # 두 번째 시트 보내는 패턴.
    # 더 단순하게: queue 에 직접 add.
    await queue.add(s1)
    await consumer._handle(s2)

    # s2 는 ticker_pending 으로 reject — cooldown 호출 안 됨
    assert checker.calls == []
    assert queue.size() == 1


def test_null_cooldown_checker_returns_none_sync():
    """NullCooldownChecker 단위 — 모든 ticker 에 대해 None."""
    import asyncio

    checker = NullCooldownChecker()
    result = asyncio.run(checker.has_recent_stoploss("005930"))
    assert result is None
