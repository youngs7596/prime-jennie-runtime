"""Stage 2 advisory policies — dispatcher / cooldown / duplicate.

design: .ai/designs/2026-05-15-cooldown-and-duplicate-guard.md §4

DB 직접 의존 (policy SQL) 부분은 fake pool 로 대체. evaluate_policies 의
dispatch / decision_log INSERT / notifier emit 흐름 검증에 집중.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel

from prime_jennie_runtime.coordinator.events import (
    EntryFilledEvent,
    EntryQueuedEvent,
    SheetPublishedEvent,
)
from prime_jennie_runtime.coordinator.policies import (
    DuplicateTodayPolicy,
    Policy,
    PolicyResult,
    RecentStoplossCooldownPolicy,
    evaluate_policies,
)

# ─── Fake infra ────────────────────────────────────────────────────────


class _FakeConn:
    def __init__(self, fetchrow_result: dict | None) -> None:
        self._fetchrow_result = fetchrow_result
        self.executed: list[tuple] = []

    async def fetchrow(self, *args: Any) -> dict | None:
        return self._fetchrow_result

    async def execute(self, *args: Any) -> None:
        self.executed.append(args)


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        pass


class _FakePool:
    """policy.evaluate 에서 acquire().fetchrow() 만, dispatcher 에서 execute() 만 호출."""

    def __init__(self, fetchrow_result: dict | None = None) -> None:
        self.conn = _FakeConn(fetchrow_result)

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self.conn)


class _CaptureNotifier:
    def __init__(self) -> None:
        self.emitted: list[BaseModel] = []

    async def emit(self, notification: BaseModel) -> str:
        self.emitted.append(notification)
        return "stub"

    async def close(self) -> None:
        pass


# ─── helpers ──────────────────────────────────────────────────────────


def _sheet_event(ticker: str = "005930", sid: str = "ps_test_005930") -> SheetPublishedEvent:
    now = datetime.now(UTC)
    return SheetPublishedEvent(
        occurred_at=now,
        correlation_id=sid,
        sheet_id=sid,
        ticker=ticker,
        strategy_tag="SECTOR_MOMENTUM",
        final_pct=0.05,
        sheet_generated_at=now,
    )


def _queued_event(ticker: str = "005930", sid: str = "ps_test_005930") -> EntryQueuedEvent:
    return EntryQueuedEvent(
        occurred_at=datetime.now(UTC),
        correlation_id=sid,
        sheet_id=sid,
        ticker=ticker,
        queue_size_after=1,
    )


def _filled_event(ticker: str = "005930", sid: str = "ps_test_005930") -> EntryFilledEvent:
    return EntryFilledEvent(
        occurred_at=datetime.now(UTC),
        correlation_id=sid,
        sheet_id=sid,
        ticker=ticker,
        filled_qty=10,
        filled_price=70_000.0,
        intended_quantity=10,
        is_partial=False,
    )


# ─── Policy: RecentStoplossCooldownPolicy ──────────────────────────────


@pytest.mark.asyncio
async def test_recent_stoploss_applies_to_sheet_and_queued_only():
    p = RecentStoplossCooldownPolicy()
    assert p.applies_to(_sheet_event()) is True
    assert p.applies_to(_queued_event()) is True
    assert p.applies_to(_filled_event()) is False


@pytest.mark.asyncio
async def test_recent_stoploss_no_history_returns_none():
    p = RecentStoplossCooldownPolicy()
    pool = _FakePool(fetchrow_result=None)
    result = await p.evaluate(pool, _sheet_event())
    assert result is None


@pytest.mark.asyncio
async def test_recent_stoploss_history_fires_advisory():
    p = RecentStoplossCooldownPolicy()
    pool = _FakePool(
        fetchrow_result={
            "executed_at": datetime.now(UTC) - timedelta(hours=2),
            "reason": "fixed_sl",
        }
    )
    result = await p.evaluate(pool, _sheet_event(ticker="001440"))
    assert result is not None
    assert result.outcome == "advisory_not_ok"
    assert result.reason == "recent_stoploss_cooldown"
    assert result.policy_name == "RecentStoplossCooldownPolicy"
    assert result.context["ticker"] == "001440"
    assert result.context["last_exit_reason"] == "fixed_sl"


# ─── Policy: DuplicateTodayPolicy ──────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_today_applies_to_sheet_only():
    p = DuplicateTodayPolicy()
    assert p.applies_to(_sheet_event()) is True
    assert p.applies_to(_queued_event()) is False
    assert p.applies_to(_filled_event()) is False


@pytest.mark.asyncio
async def test_duplicate_today_no_prior_sheet_returns_none():
    p = DuplicateTodayPolicy()
    pool = _FakePool(fetchrow_result={"n": 0})
    assert await p.evaluate(pool, _sheet_event()) is None


@pytest.mark.asyncio
async def test_duplicate_today_with_prior_sheets_fires():
    p = DuplicateTodayPolicy()
    pool = _FakePool(fetchrow_result={"n": 2})
    result = await p.evaluate(pool, _sheet_event(ticker="001440"))
    assert result is not None
    assert result.outcome == "advisory_not_ok"
    assert result.reason == "duplicate_today"
    assert result.context["earlier_sheets_today"] == 2


# ─── Dispatcher ────────────────────────────────────────────────────────


class _AlwaysFiresPolicy(Policy):
    name = "AlwaysFiresPolicy"

    def applies_to(self, event: Any) -> bool:
        return True

    async def evaluate(self, pool: Any, event: Any) -> PolicyResult | None:
        return PolicyResult(
            outcome="advisory_not_ok",
            reason="test_reason",
            policy_name=self.name,
            subject_id=getattr(event, "sheet_id", None),
            context={"ticker": getattr(event, "ticker", "?")},
        )


class _NeverFiresPolicy(Policy):
    name = "NeverFiresPolicy"

    def applies_to(self, event: Any) -> bool:
        return True

    async def evaluate(self, pool: Any, event: Any) -> PolicyResult | None:
        return None


class _CrashingPolicy(Policy):
    name = "CrashingPolicy"

    def applies_to(self, event: Any) -> bool:
        return True

    async def evaluate(self, pool: Any, event: Any) -> PolicyResult | None:
        raise RuntimeError("oops")


@pytest.mark.asyncio
async def test_dispatcher_records_decision_and_alerts_on_fire():
    pool = _FakePool()
    notifier = _CaptureNotifier()
    fired = await evaluate_policies(
        pool,
        _sheet_event(),
        notifier=notifier,
        policies=(_AlwaysFiresPolicy(),),
    )
    assert len(fired) == 1
    assert fired[0].reason == "test_reason"
    # decision_log INSERT 호출 1회
    assert len(pool.conn.executed) == 1
    # alert publish 1회
    assert len(notifier.emitted) == 1
    n = notifier.emitted[0]
    assert n.severity == "warning"
    assert "test_reason" in n.title


@pytest.mark.asyncio
async def test_dispatcher_skips_inapplicable_policies():
    pool = _FakePool()
    notifier = _CaptureNotifier()
    fired = await evaluate_policies(
        pool, _filled_event(), notifier=notifier, policies=(_NeverFiresPolicy(),)
    )
    assert fired == []
    assert notifier.emitted == []
    assert pool.conn.executed == []


@pytest.mark.asyncio
async def test_dispatcher_isolates_crash_and_continues():
    pool = _FakePool()
    notifier = _CaptureNotifier()
    fired = await evaluate_policies(
        pool,
        _sheet_event(),
        notifier=notifier,
        policies=(_CrashingPolicy(), _AlwaysFiresPolicy()),
    )
    # crashing 은 None, always_fires 만 발화 — 한 정책 crash 가 다른 정책 막지 않음
    assert len(fired) == 1
    assert fired[0].policy_name == "AlwaysFiresPolicy"


@pytest.mark.asyncio
async def test_dispatcher_works_without_notifier():
    """notifier=None 이어도 decision_log INSERT 는 진행 (alert 만 log only)."""
    pool = _FakePool()
    fired = await evaluate_policies(
        pool, _sheet_event(), notifier=None, policies=(_AlwaysFiresPolicy(),)
    )
    assert len(fired) == 1
    assert len(pool.conn.executed) == 1
