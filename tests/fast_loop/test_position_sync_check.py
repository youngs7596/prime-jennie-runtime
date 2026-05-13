"""Startup KIS-state mismatch 검사 테스트.

5-13 KIS throttle 사고 학습 — 외부 청산된 stale state 가 무한 매도 시도하는
사이클의 첫 방어선.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from prime_jennie_runtime.fast_loop.domain import PositionState
from prime_jennie_runtime.fast_loop.notifier import Notifier
from prime_jennie_runtime.fast_loop.position_sync_check import check_state_kis_mismatch
from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker
from prime_jennie_runtime.infra.redis_streams import STREAM_NOTIFICATIONS
from prime_jennie_runtime.kis_gateway.schemas import PortfolioState, Position

from .fakes import FakeKisClient


class FakeKisWithBalance(FakeKisClient):
    """KIS get_balance 를 임의 포지션으로 응답."""

    def __init__(self, tickers_in_balance: list[tuple[str, int]]):
        super().__init__()
        self._positions = [
            Position(
                stock_code=code,
                stock_name=code,
                quantity=qty,
                average_buy_price=100.0,
                total_buy_amount=100.0 * qty,
                current_price=100.0,
                current_value=100.0 * qty,
                profit_pct=0.0,
            )
            for code, qty in tickers_in_balance
        ]

    async def get_balance(self) -> PortfolioState:
        return PortfolioState(
            positions=self._positions,
            cash_balance=10_000_000,
            total_asset=10_000_000,
            stock_eval_amount=0,
            position_count=len(self._positions),
            timestamp=datetime.now(UTC),
        )


def _state(sheet_id: str, ticker: str) -> PositionState:
    return PositionState(
        sheet_id=sheet_id,
        ticker=ticker,
        entry_price=100.0,
        quantity=10,
        entered_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_state_kis_match_silent(fake_redis):
    """tracker 와 KIS 잔고 일치 시 알림 없음."""
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    await tracker.register(_state("ps_a", "005930"))
    await tracker.register(_state("ps_b", "035420"))

    kis = FakeKisWithBalance([("005930", 10), ("035420", 10)])

    result = await check_state_kis_mismatch(tracker, kis, notifier)
    assert result == {"only_in_state": [], "only_in_kis": []}
    assert await fake_redis.xlen(STREAM_NOTIFICATIONS) == 0


@pytest.mark.asyncio
async def test_state_only_triggers_critical_alert(fake_redis):
    """KIS 잔고에 없는 state ticker — critical alert (5-13 사고 핵심 시나리오)."""
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    await tracker.register(_state("ps_alive", "005930"))
    await tracker.register(_state("ps_stale_1", "028050"))  # KIS 에 없음
    await tracker.register(_state("ps_stale_2", "241560"))  # KIS 에 없음

    kis = FakeKisWithBalance([("005930", 10)])

    result = await check_state_kis_mismatch(tracker, kis, notifier)
    assert result["only_in_state"] == ["028050", "241560"]
    assert result["only_in_kis"] == []

    entries = await fake_redis.xrange(STREAM_NOTIFICATIONS)
    assert len(entries) == 1
    _msg_id, fields = entries[0]
    payload = json.loads(fields[b"payload"].decode())
    assert payload["kind"] == "alert"
    assert payload["severity"] == "critical"
    assert "mismatch" in payload["title"].lower()
    assert payload["metadata"]["only_in_state"] == ["028050", "241560"]


@pytest.mark.asyncio
async def test_kis_only_triggers_warning_alert(fake_redis):
    """state 추적 없는 KIS 보유 — warning alert (덜 위험하지만 모니터링 누락)."""
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    await tracker.register(_state("ps_alive", "005930"))

    kis = FakeKisWithBalance([("005930", 10), ("999999", 5)])

    result = await check_state_kis_mismatch(tracker, kis, notifier)
    assert result["only_in_state"] == []
    assert result["only_in_kis"] == ["999999"]

    entries = await fake_redis.xrange(STREAM_NOTIFICATIONS)
    assert len(entries) == 1
    _msg_id, fields = entries[0]
    payload = json.loads(fields[b"payload"].decode())
    assert payload["severity"] == "warning"


@pytest.mark.asyncio
async def test_balance_fetch_failure_does_not_block_startup(fake_redis):
    """KIS balance 호출 실패 시 빈 dict + WARN 로깅 (부팅 자체는 안 막음)."""
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    await tracker.register(_state("ps_a", "005930"))

    class BoomKis(FakeKisClient):
        async def get_balance(self):
            raise RuntimeError("KIS gateway 503")

    result = await check_state_kis_mismatch(tracker, BoomKis(), notifier)
    assert result == {}
    assert await fake_redis.xlen(STREAM_NOTIFICATIONS) == 0
