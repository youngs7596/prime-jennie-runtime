"""approved_buy — 추천 수락 매수 consumer 처리 (시나리오 B 설계).

핵심 정책 (2026-06-12): PAUSE 통과 (2단계 확인을 거친 승인 매수),
STOP 차단. 멱등 (시트/종목 중복 거부), 만료 시트 거부, 거부 분기 통지.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest
from pydantic import BaseModel

from prime_jennie_runtime.control.consumer import ControlCommandConsumer
from prime_jennie_runtime.fast_loop.domain import PositionState
from prime_jennie_runtime.fast_loop.entry_executor import EntryOutcome
from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker
from prime_jennie_runtime.position_sheet.schema import KST
from prime_jennie_runtime.telegram_bot.control import (
    STATE_KEY_PAUSE,
    STATE_KEY_STOP,
    ControlCommand,
)
from tests.fast_loop.test_consumer_and_tick import _sheet

# _sheet() 의 generated_at 은 2026-04-16 09:15 KST, valid_until +6h —
# 10:00 KST 는 유효기간 안.
FROZEN_NOW = datetime(2026, 4, 16, 10, 0, 0, tzinfo=KST)


class _FakeEntryExecutor:
    def __init__(self, *, success: bool = True, reason: str = "") -> None:
        self.calls: list[tuple[object, int]] = []
        self._success = success
        self._reason = reason

    async def execute(self, sheet, *, quantity: int) -> EntryOutcome:
        self.calls.append((sheet, quantity))
        return EntryOutcome(
            sheet_id=sheet.sheet_id,
            ticker=sheet.ticker,
            success=self._success,
            filled_qty=quantity if self._success else 0,
            filled_price=70_000.0 if self._success else 0.0,
            reason=self._reason,
        )


class _FakeNotifier:
    def __init__(self) -> None:
        self.emitted: list[BaseModel] = []

    async def emit(self, n):
        self.emitted.append(n)
        return "stub"


def _fetch_sheet_factory(sheet):
    async def _fetch(sheet_id):
        return [sheet] if sheet.sheet_id == sheet_id else []

    return _fetch


def _consumer(redis, *, tracker, sheet_fetcher, entry_executor, notifier=None):
    return ControlCommandConsumer(
        redis,
        consumer_name="test-approved-buy",
        tracker=tracker,
        sheet_fetcher=sheet_fetcher,
        notifier=notifier,
        entry_executor=entry_executor,
        now_fn=lambda: FROZEN_NOW,
    )


def _cmd(sheet_id: str, ticker: str = "005930", qty: int = 10) -> ControlCommand:
    return ControlCommand(
        kind="approved_buy",
        issued_by="telegram:1001",
        reason="accept_recommendation",
        payload={"sheet_id": sheet_id, "ticker": ticker, "quantity": qty},
        issued_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_approved_buy_passes_pause():
    """핵심 정책 — PAUSE 상태에서도 승인 매수는 집행된다."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        await redis.set(STATE_KEY_PAUSE, b"exit_only_human_approved_v1")
        sheet = _sheet("ps_20260416_005930_a3f2")
        executor = _FakeEntryExecutor()
        consumer = _consumer(
            redis,
            tracker=PositionTracker(redis),
            sheet_fetcher=_fetch_sheet_factory(sheet),
            entry_executor=executor,
        )
        await consumer.apply(_cmd(sheet.sheet_id))
        assert len(executor.calls) == 1
        order_sheet, qty = executor.calls[0]
        assert qty == 10
        # 시장가 강제 — echo 가 시장가 기준이므로 집행도 시장가.
        assert order_sheet.entry.trigger == "market"
        assert order_sheet.sheet_id == sheet.sheet_id
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_approved_buy_blocked_by_stop_with_alert():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        await redis.set(STATE_KEY_STOP, b"1")
        sheet = _sheet("ps_20260416_005930_a3f2")
        executor = _FakeEntryExecutor()
        notifier = _FakeNotifier()
        consumer = _consumer(
            redis,
            tracker=PositionTracker(redis),
            sheet_fetcher=_fetch_sheet_factory(sheet),
            entry_executor=executor,
            notifier=notifier,
        )
        await consumer.apply(_cmd(sheet.sheet_id))
        assert executor.calls == []
        assert any("STOP" in getattr(n, "body", "") for n in notifier.emitted)
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_approved_buy_duplicate_ticker_skipped():
    """같은 종목이 이미 추적 중이면 재수락 거부 (멱등 + 보유 중복)."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        sheet = _sheet("ps_20260416_005930_a3f2")
        tracker = PositionTracker(redis)
        await tracker.register(
            PositionState(
                sheet_id="ps_other_sheet",
                ticker="005930",
                entry_price=70_000.0,
                quantity=5,
                entered_at=FROZEN_NOW,
            )
        )
        executor = _FakeEntryExecutor()
        consumer = _consumer(
            redis,
            tracker=tracker,
            sheet_fetcher=_fetch_sheet_factory(sheet),
            entry_executor=executor,
        )
        await consumer.apply(_cmd(sheet.sheet_id))
        assert executor.calls == []
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_approved_buy_sheet_missing_alerts():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        executor = _FakeEntryExecutor()
        notifier = _FakeNotifier()

        async def _empty_fetch(sheet_id):
            return []

        consumer = _consumer(
            redis,
            tracker=PositionTracker(redis),
            sheet_fetcher=_empty_fetch,
            entry_executor=executor,
            notifier=notifier,
        )
        await consumer.apply(_cmd("ps_unknown"))
        assert executor.calls == []
        assert any("찾지 못해" in getattr(n, "body", "") for n in notifier.emitted)
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_approved_buy_ticker_mismatch_skipped():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        sheet = _sheet("ps_20260416_005930_a3f2", ticker="005930")
        executor = _FakeEntryExecutor()
        consumer = _consumer(
            redis,
            tracker=PositionTracker(redis),
            sheet_fetcher=_fetch_sheet_factory(sheet),
            entry_executor=executor,
        )
        await consumer.apply(_cmd(sheet.sheet_id, ticker="000660"))
        assert executor.calls == []
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_approved_buy_expired_sheet_alerts():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        sheet = _sheet("ps_20260416_005930_a3f2")
        executor = _FakeEntryExecutor()
        notifier = _FakeNotifier()
        consumer = ControlCommandConsumer(
            redis,
            consumer_name="test-approved-buy",
            tracker=PositionTracker(redis),
            sheet_fetcher=_fetch_sheet_factory(sheet),
            notifier=notifier,
            entry_executor=executor,
            # 유효기간 (09:15 + 6h = 15:15 KST) 이후로 시계를 돌림
            now_fn=lambda: FROZEN_NOW + timedelta(hours=6),
        )
        await consumer.apply(_cmd(sheet.sheet_id))
        assert executor.calls == []
        assert any("유효기간" in getattr(n, "body", "") for n in notifier.emitted)
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_approved_buy_invalid_payload_noop():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        sheet = _sheet("ps_20260416_005930_a3f2")
        executor = _FakeEntryExecutor()
        consumer = _consumer(
            redis,
            tracker=PositionTracker(redis),
            sheet_fetcher=_fetch_sheet_factory(sheet),
            entry_executor=executor,
        )
        await consumer.apply(_cmd(sheet.sheet_id, qty=0))
        await consumer.apply(_cmd("", qty=10))
        assert executor.calls == []
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_approved_buy_failure_alerts():
    """집행 실패 (미체결 등) 시 텔레그램 보조 통지 — 조용한 실패 금지."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        sheet = _sheet("ps_20260416_005930_a3f2")
        executor = _FakeEntryExecutor(success=False, reason="not_filled")
        notifier = _FakeNotifier()
        consumer = _consumer(
            redis,
            tracker=PositionTracker(redis),
            sheet_fetcher=_fetch_sheet_factory(sheet),
            entry_executor=executor,
            notifier=notifier,
        )
        await consumer.apply(_cmd(sheet.sheet_id))
        assert len(executor.calls) == 1
        assert any("not_filled" in getattr(n, "body", "") for n in notifier.emitted)
    finally:
        await redis.aclose()
