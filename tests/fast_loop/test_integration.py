"""Fast Loop 통합 테스트 — 시트 발행 → entry → tick → exit → notification 전체 플로우.

fakeredis + FakeKisClient + in-memory tracker로 순수 Python E2E.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from prime_jennie_runtime.fast_loop.consumer import PositionSheetConsumer
from prime_jennie_runtime.fast_loop.entry_executor import EntryExecutor
from prime_jennie_runtime.fast_loop.exit_executor import ExitExecutor
from prime_jennie_runtime.fast_loop.notifier import Notifier
from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker
from prime_jennie_runtime.fast_loop.risk_throttle import IntradayRiskThrottle
from prime_jennie_runtime.fast_loop.tick_loop import TickLoop
from prime_jennie_runtime.infra.redis_streams import (
    STREAM_NOTIFICATIONS,
    STREAM_POSITION_SHEETS,
    STREAM_PRICES,
    TypedStreamPublisher,
)
from prime_jennie_runtime.position_sheet.schema import KST, PositionSheet
from prime_jennie_runtime.slow_loop.strategy.risk_throttle import RiskThrottleSnapshot

from .fakes import FakeKisClient


def _sheet(sheet_id: str = "ps_20260416_005930_e2e1") -> PositionSheet:
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
                {"type": "trailing_tp", "activate_pct": 0.03, "drop_pct": 0.02},
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


# =====================================================================
# E2E: 시트 발행 → entry → tick → exit
# =====================================================================


@pytest.mark.asyncio
async def test_full_flow_sheet_to_entry_to_exit(fake_redis):
    """Slow loop 시트 발행 → consumer 가 큐 적재 → 첫 tick 에서 entry condition
    재평가 → 통과 시 entry → 가격 하락 tick → fixed_sl 청산."""
    from prime_jennie_runtime.fast_loop.pending_entry import (
        EntryConditionEvaluator,
        PendingEntryQueue,
    )

    tracker = PositionTracker(fake_redis)
    queue = PendingEntryQueue(fake_redis)
    evaluator = EntryConditionEvaluator()
    notifier = Notifier(fake_redis)
    kis = FakeKisClient(fill_price=70000.0)
    entry_exec = EntryExecutor(kis, tracker, notifier)
    exit_exec = ExitExecutor(kis, tracker, notifier)

    # 1. Slow loop가 시트 발행
    sheet = _sheet()
    publisher = TypedStreamPublisher(fake_redis, STREAM_POSITION_SHEETS, PositionSheet)
    await publisher.publish(sheet)

    # 2. Fast loop consumer 가 시트 소비 → 큐 적재 (즉시 매수 X)
    consumer = PositionSheetConsumer(
        fake_redis, queue, tracker, kis, group="e2e_fl", consumer="e2e_c1"
    )
    await consumer.ensure_group()
    await consumer._handle(sheet)

    assert queue.contains_sheet(sheet.sheet_id) is True
    assert kis.buy_calls == []
    assert tracker.get(sheet.sheet_id) is None
    assert kis.subscribe_calls == [["005930"]]

    # 3. tick 도착 → conditions=[] 라 즉시 entry
    async def sizer(s: PositionSheet) -> int:
        return 10

    async def fetch(sid: str) -> list[PositionSheet]:
        return [sheet] if sid == sheet.sheet_id else []

    frozen = datetime(2026, 4, 16, 9, 30, tzinfo=KST)
    loop = TickLoop(
        fake_redis,
        tracker,
        exit_exec,
        fetch,
        queue,
        evaluator,
        entry_exec,
        sizer,
        clock=lambda: frozen,
        group="e2e_tl",
        consumer="e2e_tl1",
    )
    await loop.ensure_group()

    # entry tick — 70000 (조건 없음 → 즉시 entry)
    await fake_redis.xadd(
        STREAM_PRICES,
        {
            "payload": json.dumps(
                {"ticker": "005930", "price": 70000.0, "ts": "2026-04-16T09:30:00+09:00"}
            )
        },
    )
    messages = await fake_redis.xreadgroup(
        "e2e_tl", "e2e_tl1", {STREAM_PRICES: ">"}, count=1, block=100
    )
    for _s, entries in messages:
        for msg_id, data in entries:
            await loop.process_one(msg_id, data)

    # entry 발사 + tracker 등록 + 큐에서 제거
    state = tracker.get(sheet.sheet_id)
    assert state is not None
    assert state.entry_price == 70000.0
    assert state.quantity == 10
    assert queue.contains_sheet(sheet.sheet_id) is False

    # 4. 가격 하락 tick (-6%) → fixed_sl 트리거
    kis.fill_price = 65800.0
    await fake_redis.xadd(
        STREAM_PRICES,
        {
            "payload": json.dumps(
                {"ticker": "005930", "price": 65800.0, "ts": "2026-04-16T10:00:00+09:00"}
            )
        },
    )
    messages = await fake_redis.xreadgroup(
        "e2e_tl", "e2e_tl1", {STREAM_PRICES: ">"}, count=1, block=100
    )
    for _s, entries in messages:
        for msg_id, data in entries:
            await loop.process_one(msg_id, data)

    # 5. exit 체결 + tracker 제거 확인
    assert tracker.get(sheet.sheet_id) is None
    assert len(kis.sell_calls) == 1

    # 6. notifications 스트림: entry_filled + exit_filled 두 건
    notifs_len = await fake_redis.xlen(STREAM_NOTIFICATIONS)
    assert notifs_len == 2


# =====================================================================
# Slow loop IntradayRiskThrottle 주입 회귀
# =====================================================================


@pytest.mark.asyncio
async def test_intraday_risk_throttle_satisfies_protocol(fake_redis):
    """IntradayRiskThrottle이 RiskThrottleSnapshot Protocol에 부합하는지."""
    throttle = IntradayRiskThrottle(fake_redis)
    assert isinstance(throttle, RiskThrottleSnapshot)
    # 기본값 1.0 (update 전)
    assert throttle.current_multiplier() == 1.0


@pytest.mark.asyncio
async def test_slow_loop_engine_with_intraday_throttle(fake_redis):
    """StrategyEngine에 NoOpRiskThrottle 대신 IntradayRiskThrottle 주입해도 동작."""
    from datetime import UTC, datetime

    from prime_jennie_runtime.position_sheet.schema import MacroStateSnapshot
    from prime_jennie_runtime.slow_loop.scout.schemas import EntryHint, ScreeningCandidate
    from prime_jennie_runtime.slow_loop.strategy.engine import (
        StrategyEngine,
        StrategyEngineInputs,
    )
    from prime_jennie_runtime.slow_loop.strategy.policy import load_policy

    throttle = IntradayRiskThrottle(fake_redis)
    # WARNING 수준 강제 (KOSPI -3%, VIX 37)
    await throttle.update(kospi_pct=-0.03, vix=37.0, sox_pct=0.0, now=datetime.now(UTC))

    engine = StrategyEngine(load_policy(), throttle)
    candidate = ScreeningCandidate(
        ticker="005930",
        strategy_tag="SECTOR_MOMENTUM",
        conviction=0.7,
        entry_hint=EntryHint(trigger="limit", price_hint=70000.0),
    )
    inputs = StrategyEngineInputs(
        macro_state=MacroStateSnapshot(gate="open", size_multiplier=1.0, gate_run_id="m1"),
        scout_run_id="s1",
        scout_code_hash="sha256:x",
        scout_hypothesis="test",
        generated_at=datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST),
    )
    sheet = await engine.build_sheet(candidate, inputs)
    # WARNING = 0.6 → final = 0.05 * 1.0 * 0.6 = 0.03
    assert sheet is not None
    assert sheet.size.risk_multiplier == 0.6
    assert sheet.size.final_pct == pytest.approx(0.03, abs=1e-9)
