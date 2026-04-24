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
