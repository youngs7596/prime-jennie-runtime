"""Exit Executor 단위 테스트."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from prime_jennie_runtime.control.state import SystemState
from prime_jennie_runtime.fast_loop.domain import ExitDecision, PositionState
from prime_jennie_runtime.fast_loop.exit_executor import ExitExecutor
from prime_jennie_runtime.fast_loop.notifier import Notifier
from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker
from prime_jennie_runtime.infra.redis_streams import STREAM_NOTIFICATIONS

from .fakes import FakeKisClient


def _state() -> PositionState:
    return PositionState(
        sheet_id="ps_20260416_005930_a3f2",
        ticker="005930",
        entry_price=70000.0,
        quantity=10,
        entered_at=datetime.now(UTC),
        high_watermark=70000.0,
    )


@pytest.mark.asyncio
async def test_exit_full_close(fake_redis):
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient(fill_price=66500.0)
    executor = ExitExecutor(kis, tracker, notifier)

    state = _state()
    await tracker.register(state)

    decision = ExitDecision(should_close=True, reason="fixed_sl", portion=1.0)
    outcome = await executor.execute(state, decision)

    assert outcome.success is True
    assert outcome.fully_closed is True
    assert outcome.closed_qty == 10
    # tracker에서 제거됨
    assert tracker.get(state.sheet_id) is None
    # 시장가 sell
    assert kis.sell_calls[0].order_type == "market"
    # notification
    assert await fake_redis.xlen(STREAM_NOTIFICATIONS) == 1


@pytest.mark.asyncio
async def test_exit_scale_out_partial(fake_redis):
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient(fill_price=72500.0)
    executor = ExitExecutor(kis, tracker, notifier)

    state = _state()
    await tracker.register(state)

    decision = ExitDecision(should_close=True, reason="scale_out", portion=0.25)
    # 10 * 0.25 = 2.5 → min(2, 10)=2 예상 (int 변환)
    kis.fill_qty_override = {"SELL000001": 2}
    outcome = await executor.execute(state, decision)

    assert outcome.success is True
    assert outcome.fully_closed is False
    assert outcome.closed_qty == 2
    # 남은 수량 상태 유지
    remaining_state = tracker.get(state.sheet_id)
    assert remaining_state is not None
    assert remaining_state.quantity == 8


@pytest.mark.asyncio
async def test_exit_partial_fill_cancels_remainder_and_keeps_sheet(fake_redis):
    """전량청산 결정이 부분 체결되면: 미체결 잔량 취소 + sheet 를 잔여 수량으로 유지.

    e708586 entry partial-fill fix 의 exit 측 대칭. 잔량 주문을 남기면 추적 밖
    체결로 보유수량이 어긋나거나 다음 tick 에 이중 매도가 날 수 있다.
    """
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient(fill_price=66500.0)
    executor = ExitExecutor(kis, tracker, notifier)

    state = _state()  # quantity 10
    await tracker.register(state)

    decision = ExitDecision(should_close=True, reason="fixed_sl", portion=1.0)
    # confirm 시점 6/10 부분 체결, 취소 후 재조회(terminal)도 6.
    kis.confirm_qty_override = {"SELL000001": 6}
    kis.fill_qty_override = {"SELL000001": 6}
    outcome = await executor.execute(state, decision)

    assert outcome.success is True
    assert outcome.fully_closed is False  # 잔량 남음 → sheet 유지
    assert outcome.closed_qty == 6
    # 미체결 잔량 취소됨
    assert kis.cancel_calls == ["SELL000001"]
    # sheet 는 잔여 4주로 살아있음 (다음 tick 에 exit rule 이 재청산)
    remaining = tracker.get(state.sheet_id)
    assert remaining is not None
    assert remaining.quantity == 4


@pytest.mark.asyncio
async def test_breakeven_sl_raise_no_order(fake_redis):
    """should_close=False + new_sl_price → sell 주문 안 함, state.breakeven_sl_price 갱신."""
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient()
    executor = ExitExecutor(kis, tracker, notifier)

    state = _state()
    await tracker.register(state)

    decision = ExitDecision(
        should_close=False,
        reason="breakeven_sl_raise",
        new_sl_price=70210.0,
    )
    outcome = await executor.execute(state, decision)

    assert outcome.success is True
    assert outcome.closed_qty == 0
    assert kis.sell_calls == []
    # state 갱신
    restored = tracker.get(state.sheet_id)
    assert restored.breakeven_sl_price == 70210.0
    # notification 없음
    assert await fake_redis.xlen(STREAM_NOTIFICATIONS) == 0


@pytest.mark.asyncio
async def test_exit_sell_not_filled(fake_redis):
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient(should_fill=False)
    executor = ExitExecutor(kis, tracker, notifier)

    state = _state()
    await tracker.register(state)
    decision = ExitDecision(should_close=True, reason="fixed_sl", portion=1.0)
    outcome = await executor.execute(state, decision)

    assert outcome.success is False
    # tracker 유지 (아직 미청산)
    restored = tracker.get(state.sheet_id)
    assert restored is not None
    # fail_count 증가 — 한 번 실패 후 threshold (3) 미달
    assert restored.exit_fail_count == 1


@pytest.mark.asyncio
async def test_exit_auto_close_after_consecutive_failures(fake_redis):
    """sell_not_filled 가 max_exit_failures 연속 발생하면 sheet auto-close + alert.

    5-13 stale state 사고 (외부 청산된 sheet 의 무한 매도 시도) 회귀 방지.
    """
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient(should_fill=False)
    executor = ExitExecutor(kis, tracker, notifier, max_exit_failures=3)

    state = _state()
    await tracker.register(state)
    decision = ExitDecision(should_close=True, reason="profit_floor", portion=1.0)

    # 1, 2회 실패 — sheet 유지
    for expected_count in (1, 2):
        outcome = await executor.execute(state, decision)
        assert outcome.success is False
        restored = tracker.get(state.sheet_id)
        assert restored is not None
        assert restored.exit_fail_count == expected_count

    # 3회째 실패 — auto-close + alert
    outcome = await executor.execute(state, decision)
    assert outcome.success is False
    assert tracker.get(state.sheet_id) is None

    # alert notification 발행 확인
    entries = await fake_redis.xrange(STREAM_NOTIFICATIONS)
    assert len(entries) == 1
    _msg_id, fields = entries[0]
    payload = json.loads(fields[b"payload"].decode())
    assert payload["kind"] == "alert"
    assert payload["severity"] == "critical"
    assert "abandoned" in payload["title"]
    assert payload["metadata"]["sheet_id"] == state.sheet_id
    assert payload["metadata"]["fail_count"] == 3


@pytest.mark.asyncio
async def test_exit_fail_count_reset_on_success(fake_redis):
    """매도 성공 시 fail_count 리셋되어 후속 실패가 재계산.

    장 마감 임박 미체결 등 일시 실패가 누적되어 정상 sheet 가 잘못 close
    되는 일을 막는다.
    """
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient(should_fill=False, fill_price=66500.0)
    executor = ExitExecutor(kis, tracker, notifier, max_exit_failures=3)

    state = _state()
    await tracker.register(state)
    decision = ExitDecision(should_close=True, reason="fixed_sl", portion=1.0)

    # 2회 실패 누적
    await executor.execute(state, decision)
    await executor.execute(state, decision)
    restored = tracker.get(state.sheet_id)
    assert restored is not None
    assert restored.exit_fail_count == 2

    # 부분 체결 성공 (scale_out) — fail_count 리셋
    kis.should_fill = True
    kis.fill_qty_override = {"SELL000003": 5}
    partial_decision = ExitDecision(should_close=True, reason="scale_out", portion=0.5)
    outcome = await executor.execute(state, partial_decision)
    assert outcome.success is True
    restored = tracker.get(state.sheet_id)
    assert restored is not None
    assert restored.exit_fail_count == 0


@pytest.mark.asyncio
async def test_exit_blocked_by_stop(fake_redis):
    """STOP 키가 설정돼 있으면 exit 주문 발행 거부 (사용자 요청 STOP 의 진짜 의미)."""
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient(fill_price=66500.0)
    system_state = SystemState(fake_redis)
    executor = ExitExecutor(kis, tracker, notifier, system_state=system_state)

    # STOP 활성화
    await fake_redis.set("control.state:stop", b"1")

    state = _state()
    await tracker.register(state)

    decision = ExitDecision(should_close=True, reason="fixed_sl", portion=1.0)
    outcome = await executor.execute(state, decision)

    assert outcome.success is False
    assert outcome.reason.startswith("blocked_stop")
    assert kis.sell_calls == []  # KIS 호출 자체 안 함
    # tracker 그대로 유지 — 청산 미실행
    assert tracker.get(state.sheet_id) is not None


@pytest.mark.asyncio
async def test_exit_sl_raise_unaffected_by_stop(fake_redis):
    """SL 상향 (주문 미동반) 은 STOP 영향 없이 진행."""
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient()
    system_state = SystemState(fake_redis)
    executor = ExitExecutor(kis, tracker, notifier, system_state=system_state)

    await fake_redis.set("control.state:stop", b"1")

    state = _state()
    await tracker.register(state)
    decision = ExitDecision(should_close=False, reason="breakeven_sl_raise", new_sl_price=70210.0)
    outcome = await executor.execute(state, decision)
    assert outcome.success is True
    restored = tracker.get(state.sheet_id)
    assert restored.breakeven_sl_price == 70210.0


@pytest.mark.asyncio
async def test_exit_notification_carries_pnl_and_stock_name(fake_redis):
    """청산 알림에 pnl_amount/pnl_pct/entry_avg_price/stock_name 이 채워져야 함."""
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient(fill_price=66500.0)

    async def resolver(stock_code: str) -> str:
        return "삼성전자" if stock_code == "005930" else stock_code

    executor = ExitExecutor(kis, tracker, notifier, stock_resolver=resolver)
    state = _state()
    await tracker.register(state)

    decision = ExitDecision(should_close=True, reason="fixed_sl", portion=1.0)
    outcome = await executor.execute(state, decision)

    assert outcome.success is True
    entries = await fake_redis.xrange(STREAM_NOTIFICATIONS)
    assert len(entries) == 1
    _msg_id, fields = entries[0]
    payload = json.loads(fields[b"payload"].decode())
    assert payload["kind"] == "exit_filled"
    assert payload["stock_name"] == "삼성전자"
    assert payload["entry_avg_price"] == 70000.0
    # (66500 - 70000) * 10 = -35,000
    assert payload["pnl_amount"] == pytest.approx(-35000.0)
    # (66500/70000 - 1) * 100 = -5.0
    assert payload["pnl_pct"] == pytest.approx(-5.0)


@pytest.mark.asyncio
async def test_exit_dryrun_skips_kis(fake_redis):
    """DRY_RUN 키 ON 이면 KIS sell 호출 없이 시뮬레이션 outcome + notification."""
    tracker = PositionTracker(fake_redis)
    notifier = Notifier(fake_redis)
    kis = FakeKisClient(fill_price=66500.0)
    system_state = SystemState(fake_redis)
    executor = ExitExecutor(kis, tracker, notifier, system_state=system_state)

    await fake_redis.set("control.state:dryrun", b"1")

    state = _state()
    await tracker.register(state)

    decision = ExitDecision(should_close=True, reason="fixed_sl", portion=1.0)
    outcome = await executor.execute(state, decision)

    assert outcome.success is True
    assert outcome.reason.startswith("dryrun:")
    assert outcome.closed_qty == 10
    assert kis.sell_calls == []  # KIS 호출 0
    # dryrun 도 notification 은 발행 (사용자 가시성)
    assert await fake_redis.xlen(STREAM_NOTIFICATIONS) == 1
