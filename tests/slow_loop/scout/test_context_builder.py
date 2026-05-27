"""ScoutContextBuilder stub feeder 기반 테스트."""

from __future__ import annotations

from datetime import date

import pytest

from prime_jennie_runtime.position_sheet.schema import ALLOWED_STRATEGY_TAGS
from prime_jennie_runtime.slow_loop.scout.context_builder import ScoutContextBuilder
from prime_jennie_runtime.slow_loop.scout.feeders.stub import (
    StubMarketSummaryFeeder,
    StubNewsEventFeeder,
    StubSectorMomentumFeeder,
    StubUniverseFeeder,
)
from prime_jennie_runtime.slow_loop.scout.schemas import MacroStateForScout


@pytest.mark.asyncio
async def test_builder_assembles_context():
    builder = ScoutContextBuilder(
        universe=StubUniverseFeeder(),
        news=StubNewsEventFeeder(),
        sector=StubSectorMomentumFeeder(),
        market=StubMarketSummaryFeeder(),
    )
    macro = MacroStateForScout(
        gate="open",
        size_multiplier=0.75,
        gate_run_id="macro_20260416_0800",
    )
    ctx = await builder.build(as_of=date(2026, 4, 16), macro_state=macro)

    assert len(ctx.universe) == 5
    assert "005930" in ctx.universe
    # 모든 universe ticker에 대해 뉴스 이벤트 entry 존재
    assert set(ctx.news_events.keys()) == set(ctx.universe)
    assert ctx.macro_state.gate == "open"
    assert set(ctx.strategy_tags_available) == ALLOWED_STRATEGY_TAGS
    assert ctx.trigger_reason == "scheduled_0830"


@pytest.mark.asyncio
async def test_custom_trigger_reason():
    builder = ScoutContextBuilder(
        universe=StubUniverseFeeder(),
        news=StubNewsEventFeeder(),
        sector=StubSectorMomentumFeeder(),
        market=StubMarketSummaryFeeder(),
    )
    macro = MacroStateForScout(gate="open", size_multiplier=1.0, gate_run_id="m1")
    ctx = await builder.build(
        as_of=date(2026, 4, 16),
        macro_state=macro,
        trigger_reason="manual",
    )
    assert ctx.trigger_reason == "manual"


@pytest.mark.asyncio
async def test_previous_outcomes_empty_when_no_engine():
    """engine 미주입 — previous_outcomes 빈 list (fail-open)."""
    builder = ScoutContextBuilder(
        universe=StubUniverseFeeder(),
        news=StubNewsEventFeeder(),
        sector=StubSectorMomentumFeeder(),
        market=StubMarketSummaryFeeder(),
    )
    macro = MacroStateForScout(gate="open", size_multiplier=1.0, gate_run_id="m1")
    ctx = await builder.build(as_of=date(2026, 4, 16), macro_state=macro)
    assert ctx.previous_outcomes == []


@pytest.mark.asyncio
async def test_previous_outcomes_failopen_on_db_error():
    """DB 장애 / view 미존재 — fail-open 으로 빈 list 반환, Scout 진행 보장."""
    from prime_jennie_runtime.slow_loop.scout.context_builder import _fetch_previous_outcomes

    class _BrokenEngine:
        def connect(self):
            raise RuntimeError("view scout_outcomes_v1 does not exist")

    result = await _fetch_previous_outcomes(_BrokenEngine())
    assert result == []


@pytest.mark.asyncio
async def test_today_exits_empty_when_no_engine():
    """engine 미주입 — recently_exited_today_tickers 빈 list (fail-open)."""
    builder = ScoutContextBuilder(
        universe=StubUniverseFeeder(),
        news=StubNewsEventFeeder(),
        sector=StubSectorMomentumFeeder(),
        market=StubMarketSummaryFeeder(),
    )
    macro = MacroStateForScout(gate="open", size_multiplier=1.0, gate_run_id="m1")
    ctx = await builder.build(as_of=date(2026, 4, 16), macro_state=macro)
    assert ctx.recently_exited_today_tickers == []


@pytest.mark.asyncio
async def test_today_exits_failopen_on_db_error():
    """DB 장애 — fail-open 으로 빈 list 반환."""
    from prime_jennie_runtime.slow_loop.scout.context_builder import _fetch_today_exits

    class _BrokenEngine:
        def connect(self):
            raise RuntimeError("db down")

    result = await _fetch_today_exits(_BrokenEngine())
    assert result == []


@pytest.mark.asyncio
async def test_held_positions_empty_when_no_engine():
    """Coordinator A1 v1 — engine 미주입 시 held_positions 빈 list (fail-open)."""
    builder = ScoutContextBuilder(
        universe=StubUniverseFeeder(),
        news=StubNewsEventFeeder(),
        sector=StubSectorMomentumFeeder(),
        market=StubMarketSummaryFeeder(),
    )
    macro = MacroStateForScout(gate="open", size_multiplier=1.0, gate_run_id="m1")
    ctx = await builder.build(as_of=date(2026, 4, 16), macro_state=macro)
    assert ctx.held_positions == []


@pytest.mark.asyncio
async def test_held_positions_injected_from_state_hub(monkeypatch):
    """get_held_positions 결과가 ScoutContext.held_positions 로 그대로 주입된다."""
    from prime_jennie_runtime.coordinator.state import HeldPositionSummary
    from prime_jennie_runtime.slow_loop.scout import context_builder as cb_mod

    fixture = [
        HeldPositionSummary(
            ticker="005930",
            name="삼성전자",
            quantity=100,
            average_buy_price=70_000,
            source="mixed",
            open_sheet_ids=["ps_x"],
        ),
    ]

    async def fake_get_held(engine, *, ticker=None):
        return fixture

    monkeypatch.setattr(cb_mod, "get_held_positions", fake_get_held)

    builder = ScoutContextBuilder(
        universe=StubUniverseFeeder(),
        news=StubNewsEventFeeder(),
        sector=StubSectorMomentumFeeder(),
        market=StubMarketSummaryFeeder(),
        engine=object(),  # truthy — fake_get_held 가 결과 반환
    )
    macro = MacroStateForScout(gate="open", size_multiplier=1.0, gate_run_id="m1")
    ctx = await builder.build(as_of=date(2026, 4, 16), macro_state=macro)

    assert len(ctx.held_positions) == 1
    assert ctx.held_positions[0].ticker == "005930"
    assert ctx.held_positions[0].source == "mixed"


@pytest.mark.asyncio
async def test_held_positions_failopen_on_state_error(monkeypatch):
    """State Hub 가 예외 던지면 빈 list 로 fail-open — Scout 진행 보장."""
    from prime_jennie_runtime.slow_loop.scout import context_builder as cb_mod

    async def boom(engine, *, ticker=None):
        raise RuntimeError("state hub down")

    monkeypatch.setattr(cb_mod, "get_held_positions", boom)

    builder = ScoutContextBuilder(
        universe=StubUniverseFeeder(),
        news=StubNewsEventFeeder(),
        sector=StubSectorMomentumFeeder(),
        market=StubMarketSummaryFeeder(),
        engine=object(),
    )
    macro = MacroStateForScout(gate="open", size_multiplier=1.0, gate_run_id="m1")
    ctx = await builder.build(as_of=date(2026, 4, 16), macro_state=macro)
    assert ctx.held_positions == []
