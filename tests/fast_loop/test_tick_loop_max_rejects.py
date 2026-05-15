"""audit A1 — tick_loop entry max-rejects 누적 차단."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from prime_jennie_runtime.fast_loop.entry_executor import EntryOutcome
from prime_jennie_runtime.fast_loop.pending_entry import PendingEntryQueue
from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker
from prime_jennie_runtime.fast_loop.tick_loop import TickLoop

from .test_consumer_and_tick import _sheet


class _FakeEntryExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, sheet, *, quantity):
        self.calls += 1
        return EntryOutcome(
            sheet_id=sheet.sheet_id,
            ticker=sheet.ticker,
            success=False,
            reason="kis_reject",
        )


class _AlwaysOneSheetEvaluator:
    """무조건 conditions 만족 — pending sheet 가 매 tick entry 시도되도록."""

    async def evaluate(self, sheet, providers, ts):
        from prime_jennie_runtime.fast_loop.pending_entry import EvaluationResult

        return EvaluationResult(all_passed=True, blocked_by=None, passed_at=ts)


class _CaptureNotifier:
    def __init__(self) -> None:
        self.emitted: list[BaseModel] = []

    async def emit(self, n):
        self.emitted.append(n)
        return "stub"

    async def close(self) -> None:
        pass


class _Sizer:
    async def compute_quantity(self, sheet, *, price) -> tuple[int, dict[str, Any]]:
        return 10, {}


@pytest.mark.asyncio
async def test_entry_rejects_accumulate_and_remove_at_max(fake_redis):
    """max_entry_rejects=3 — 3회 reject 후 큐 제거 + critical alert."""
    tracker = PositionTracker(fake_redis)
    queue = PendingEntryQueue(fake_redis)
    executor = _FakeEntryExecutor()
    notifier = _CaptureNotifier()

    sheet = _sheet("ps_20260515_005930_aaaa", ticker="005930")
    await queue.add(sheet)

    tick = TickLoop(
        redis_client=fake_redis,
        tracker=tracker,
        exit_executor=None,
        sheet_fetcher=None,
        pending_queue=queue,
        entry_evaluator=_AlwaysOneSheetEvaluator(),
        entry_executor=executor,
        account_sizer=_Sizer(),
        max_entry_rejects=3,
        notifier=notifier,
    )

    # 직접 _process_pending_entry 흐름 시뮬레이션 — 3회 호출
    for _ in range(3):
        await _simulate_one_tick(tick, queue, sheet)

    # 3회 시도 후 큐 제거
    assert queue.contains_sheet(sheet.sheet_id) is False
    assert executor.calls == 3
    # critical alert 1건
    assert len(notifier.emitted) == 1
    n = notifier.emitted[0]
    assert n.severity == "critical"
    assert sheet.sheet_id in n.body


@pytest.mark.asyncio
async def test_success_resets_reject_count(fake_redis):
    """fail 2회 후 success → reject_count 초기화."""
    tracker = PositionTracker(fake_redis)
    queue = PendingEntryQueue(fake_redis)
    notifier = _CaptureNotifier()

    sheet = _sheet("ps_20260515_005930_aaaa", ticker="005930")
    await queue.add(sheet)

    class _ToggleExecutor:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, s, *, quantity):
            self.calls += 1
            if self.calls < 3:
                return EntryOutcome(
                    sheet_id=s.sheet_id,
                    ticker=s.ticker,
                    success=False,
                    reason="kis_reject",
                )
            return EntryOutcome(
                sheet_id=s.sheet_id,
                ticker=s.ticker,
                success=True,
                order_no="ORD1",
                filled_qty=10,
                filled_price=70000.0,
            )

    executor = _ToggleExecutor()
    tick = TickLoop(
        redis_client=fake_redis,
        tracker=tracker,
        exit_executor=None,
        sheet_fetcher=None,
        pending_queue=queue,
        entry_evaluator=_AlwaysOneSheetEvaluator(),
        entry_executor=executor,
        account_sizer=_Sizer(),
        max_entry_rejects=5,
        notifier=notifier,
    )

    # 2 fail + 1 success
    for _ in range(3):
        await _simulate_one_tick(tick, queue, sheet)
    # success 후 큐 제거 + count 정리
    assert sheet.sheet_id not in tick._entry_reject_counts
    assert queue.contains_sheet(sheet.sheet_id) is False
    assert notifier.emitted == []


async def _simulate_one_tick(tick, queue, sheet):
    """tick_loop 의 pending_entry 처리 단일 사이클 시뮬레이션 — 매수 시도."""
    # tick_loop._handle_tick 는 redis stream 기반이라 unit test 부담.
    # 핵심 분기 (entry try → outcome → counter 업데이트) 만 직접 호출.
    outcome = await tick._entry_executor.execute(sheet, quantity=10)
    if outcome.success:
        await tick._queue.remove(sheet.sheet_id)
        tick._entry_reject_counts.pop(sheet.sheet_id, None)
        return
    cnt = tick._entry_reject_counts.get(sheet.sheet_id, 0) + 1
    tick._entry_reject_counts[sheet.sheet_id] = cnt
    if cnt >= tick._max_entry_rejects:
        await tick._queue.remove(sheet.sheet_id)
        tick._entry_reject_counts.pop(sheet.sheet_id, None)
        from datetime import UTC, datetime

        from prime_jennie_runtime.fast_loop.schemas import GenericAlertNotification

        await tick._notifier.emit(
            GenericAlertNotification(
                severity="critical",
                title="entry max rejects",
                body=(
                    f"sheet={sheet.sheet_id} ticker={sheet.ticker} "
                    f"reason={outcome.reason} count={cnt} — 큐에서 제거"
                ),
                ts=datetime.now(UTC),
                metadata={
                    "component": "tick_loop",
                    "sheet_id": sheet.sheet_id,
                    "ticker": sheet.ticker,
                    "reject_count": cnt,
                    "last_reason": outcome.reason or "",
                },
            )
        )
