"""Slow Loop E2E 테스트 — StubChatModel + stub feeders + screening stub.

3 시나리오:
1. 정상 open → 3 candidates → 3 sheets published
2. Macro closed → 시트 0개 (skipped_macro_closed)
3. Scout hallucination 50% → skipped (run 실패는 아니지만 skipped_reason='scout_hallucination')
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from minyoung_mah import (
    CollectingObserver,
    NullHITLChannel,
    NullMemoryStore,
    Orchestrator,
    RoleRegistry,
    SingleModelRouter,
    ToolRegistry,
    default_resilience,
)

from prime_jennie_runtime.infra.redis_streams import STREAM_POSITION_SHEETS
from prime_jennie_runtime.position_sheet.schema import KST
from prime_jennie_runtime.slow_loop.macro.context_builder import MacroContextBuilder
from prime_jennie_runtime.slow_loop.macro.feeders.stub import (
    StubKorMacroNewsFeeder,
    StubMarketSnapshotFeeder,
    StubWsjDigestFeeder,
)
from prime_jennie_runtime.slow_loop.macro.role import MacroGateRole
from prime_jennie_runtime.slow_loop.macro.schemas import MacroGateOutput
from prime_jennie_runtime.slow_loop.macro.state_store import MacroStateStore
from prime_jennie_runtime.slow_loop.pipeline import SlowLoopComponents, run_slow_loop
from prime_jennie_runtime.slow_loop.scout.context_builder import ScoutContextBuilder
from prime_jennie_runtime.slow_loop.scout.feeders.stub import (
    StubMarketSummaryFeeder,
    StubNewsEventFeeder,
    StubSectorMomentumFeeder,
    StubUniverseFeeder,
)
from prime_jennie_runtime.slow_loop.scout.role import ScoutRole
from prime_jennie_runtime.slow_loop.scout.schemas import (
    EntryHint,
    ScoutOutput,
    ScreeningCandidate,
)
from prime_jennie_runtime.slow_loop.scout.screening_stub import ScreeningToolAdapterStub
from prime_jennie_runtime.slow_loop.strategy.engine import StrategyEngine
from prime_jennie_runtime.slow_loop.strategy.policy import load_policy
from prime_jennie_runtime.slow_loop.strategy.publisher import PositionSheetPublisher
from prime_jennie_runtime.slow_loop.strategy.risk_throttle import (
    NoOpRiskThrottle,
    RiskThrottleSnapshot,
)

from ._stubs import StubChatModel

# =====================================================================
# 공통 fixtures
# =====================================================================


def _default_scout_output() -> ScoutOutput:
    return ScoutOutput(
        screening_code="def screen(market_data, context):\n    return []\n",
        hypothesis="반도체 모멘텀 가설",
        expected_candidates=3,
        factor_weights={"momentum": 0.4, "news": 0.3, "eps_rev": 0.3},
        strategy_tags_used=["SECTOR_MOMENTUM"],
        fallback_strategy="skip_today",
        estimated_runtime_seconds=5.0,
    )


def _default_macro_output(gate: str = "open", size: float = 0.75) -> MacroGateOutput:
    return MacroGateOutput(
        gate=gate,  # type: ignore[arg-type]
        size_multiplier=size,
        reasoning="test",
        top_risks=[],
        confidence="medium",
        news_digest_ref="wsj_stub_0000",
    )


def _make_components(
    fake_redis,
    observer,
    scout_out: ScoutOutput,
    macro_out: MacroGateOutput,
    screening_candidates: list[ScreeningCandidate] | None = None,
    risk_throttle: RiskThrottleSnapshot | None = None,
    system_state=None,
) -> SlowLoopComponents:
    stub_model = StubChatModel({ScoutOutput: scout_out, MacroGateOutput: macro_out})

    orch = Orchestrator(
        role_registry=RoleRegistry.of(ScoutRole(), MacroGateRole()),
        tool_registry=ToolRegistry(),
        model_router=SingleModelRouter(stub_model),
        memory=NullMemoryStore(),
        hitl=NullHITLChannel(),
        observer=observer,
        resilience=default_resilience(),
    )

    scout_builder = ScoutContextBuilder(
        universe=StubUniverseFeeder(),
        news=StubNewsEventFeeder(),
        sector=StubSectorMomentumFeeder(),
        market=StubMarketSummaryFeeder(),
    )
    macro_builder = MacroContextBuilder(
        wsj=StubWsjDigestFeeder(),
        market=StubMarketSnapshotFeeder(),
        kor=StubKorMacroNewsFeeder(),
    )

    screening = (
        ScreeningToolAdapterStub()
        if screening_candidates is None
        else ScreeningToolAdapterStub(candidates=screening_candidates)
    )

    engine = StrategyEngine(load_policy(), risk_throttle or NoOpRiskThrottle())
    publisher = PositionSheetPublisher(fake_redis)
    state_store = MacroStateStore(fake_redis)

    return SlowLoopComponents(
        orchestrator=orch,
        scout_builder=scout_builder,
        macro_builder=macro_builder,
        screening=screening,
        engine=engine,
        publisher=publisher,
        state_store=state_store,
        observer=observer,
        system_state=system_state,
    )


# =====================================================================
# 1. 정상 경로
# =====================================================================


@pytest.mark.asyncio
async def test_normal_flow_publishes_three_sheets(fake_redis):
    observer = CollectingObserver()
    comp = _make_components(
        fake_redis,
        observer,
        _default_scout_output(),
        _default_macro_output(gate="open", size=0.75),
    )
    result = await run_slow_loop(
        comp,
        as_of_date=date(2026, 4, 16),
        as_of_dt=datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST),
        macro_run_id="macro_20260416_0800",
        scout_run_id="scout_20260416_0830",
    )
    assert result.skipped_reason is None
    assert len(result.sheets_published) == 3
    assert result.macro_post is not None
    assert result.macro_post.output.gate == "open"
    assert result.macro_post.output.size_multiplier == 0.75
    assert result.scout_output is not None

    # Redis에 3건 발행
    length = await fake_redis.xlen(STREAM_POSITION_SHEETS)
    assert length == 3

    # pj.* 이벤트 최소 집합 확인
    names = {e.name for e in observer.events}
    assert "pj.scout.code_generated" in names
    assert "pj.strategy.sheet_published" in names


# =====================================================================
# 2. Macro closed — Scout 생략, 시트 0개
# =====================================================================


@pytest.mark.asyncio
async def test_macro_closed_skips_scout_no_sheets(fake_redis):
    observer = CollectingObserver()
    comp = _make_components(
        fake_redis,
        observer,
        _default_scout_output(),
        _default_macro_output(gate="closed", size=0.0),
    )
    result = await run_slow_loop(
        comp,
        as_of_date=date(2026, 4, 16),
        as_of_dt=datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST),
        macro_run_id="macro_20260416_0800",
        scout_run_id="scout_20260416_0830",
    )
    assert result.skipped_reason == "macro_closed"
    assert result.sheets_published == []

    # Redis 시트 0건
    length = await fake_redis.xlen(STREAM_POSITION_SHEETS)
    assert length == 0

    names = {e.name for e in observer.events}
    assert "pj.macro.gate_closed" in names
    assert "pj.slow_loop.skipped_macro_closed" in names
    assert "pj.scout.code_generated" not in names  # Scout 생략


# =====================================================================
# 3. Scout hallucination 50% → skipped
# =====================================================================


@pytest.mark.asyncio
async def test_scout_hallucination_halts_publishing(fake_redis):
    observer = CollectingObserver()

    # StubUniverseFeeder는 5 ticker 반환. 절반 이상을 universe 밖으로.
    hallucinated = [
        ScreeningCandidate(
            ticker="999998",
            strategy_tag="SECTOR_MOMENTUM",
            conviction=0.8,
            entry_hint=EntryHint(trigger="market"),
        ),
        ScreeningCandidate(
            ticker="999997",
            strategy_tag="SECTOR_MOMENTUM",
            conviction=0.7,
            entry_hint=EntryHint(trigger="market"),
        ),
        ScreeningCandidate(
            ticker="999996",
            strategy_tag="SECTOR_MOMENTUM",
            conviction=0.65,
            entry_hint=EntryHint(trigger="market"),
        ),
        # 하나만 정상
        ScreeningCandidate(
            ticker="005930",
            strategy_tag="SECTOR_MOMENTUM",
            conviction=0.9,
            entry_hint=EntryHint(trigger="limit", price_hint=71200.0),
        ),
    ]
    # 3/4 = 75% universe 밖 → fail (>=50%)

    comp = _make_components(
        fake_redis,
        observer,
        _default_scout_output(),
        _default_macro_output(gate="open", size=1.0),
        screening_candidates=hallucinated,
    )
    result = await run_slow_loop(
        comp,
        as_of_date=date(2026, 4, 16),
        as_of_dt=datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST),
        macro_run_id="macro_20260416_0800",
        scout_run_id="scout_20260416_0830",
    )
    assert result.skipped_reason == "scout_hallucination"
    assert result.sheets_published == []
    assert result.validation is not None
    assert result.validation.hallucination_fail is True
    assert len(result.validation.hallucinated_tickers) == 3

    names = {e.name for e in observer.events}
    assert "pj.scout.hallucination_suspected" in names


# =====================================================================
# 4. Candidates 0개 → no_candidates
# =====================================================================


@pytest.mark.asyncio
async def test_zero_candidates_skipped(fake_redis):
    observer = CollectingObserver()
    comp = _make_components(
        fake_redis,
        observer,
        _default_scout_output(),
        _default_macro_output(gate="open", size=1.0),
        screening_candidates=[],
    )
    result = await run_slow_loop(
        comp,
        as_of_date=date(2026, 4, 16),
        as_of_dt=datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST),
        macro_run_id="macro_20260416_0800",
        scout_run_id="scout_20260416_0830",
    )
    assert result.skipped_reason == "no_candidates"
    assert result.sheets_published == []
    names = {e.name for e in observer.events}
    assert "pj.scout.no_candidates" in names


# =====================================================================
# 5. control.state — STOP/PAUSE 시 sheet 발행 차단
# =====================================================================


@pytest.mark.asyncio
async def test_control_stopped_blocks_publishing(fake_redis):
    """SystemState.stopped == True → Macro/Scout LLM 호출 모두 skip + 시트 0개."""
    from prime_jennie_runtime.control.state import SystemState
    from prime_jennie_runtime.infra.redis_streams import STREAM_POSITION_SHEETS
    from prime_jennie_runtime.telegram_bot.control import STATE_KEY_STOP

    observer = CollectingObserver()
    await fake_redis.set(STATE_KEY_STOP, "1")

    comp = _make_components(
        fake_redis,
        observer,
        _default_scout_output(),
        _default_macro_output(gate="open", size=1.0),
        system_state=SystemState(fake_redis),
    )
    result = await run_slow_loop(
        comp,
        as_of_date=date(2026, 4, 16),
        as_of_dt=datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST),
        macro_run_id="macro_20260416_0800",
        scout_run_id="scout_20260416_0830",
    )
    assert result.skipped_reason == "control_stopped"
    assert result.sheets_published == []
    # Macro/Scout 둘 다 skip — 시트도 발행 X
    assert await fake_redis.xlen(STREAM_POSITION_SHEETS) == 0

    names = {e.name for e in observer.events}
    assert "pj.slow_loop.skipped_control" in names
    # Macro/Scout LLM 도 호출 전 — code_generated 이벤트 없음
    assert "pj.scout.code_generated" not in names


@pytest.mark.asyncio
async def test_control_paused_blocks_publishing(fake_redis):
    """SystemState.paused (pause_reason 있음) → skip."""
    from prime_jennie_runtime.control.state import SystemState
    from prime_jennie_runtime.telegram_bot.control import STATE_KEY_PAUSE

    observer = CollectingObserver()
    await fake_redis.set(STATE_KEY_PAUSE, "manual")

    comp = _make_components(
        fake_redis,
        observer,
        _default_scout_output(),
        _default_macro_output(gate="open", size=1.0),
        system_state=SystemState(fake_redis),
    )
    result = await run_slow_loop(
        comp,
        as_of_date=date(2026, 4, 16),
        as_of_dt=datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST),
        macro_run_id="macro_20260416_0800",
        scout_run_id="scout_20260416_0830",
    )
    assert result.skipped_reason == "control_paused"
    assert result.sheets_published == []


@pytest.mark.asyncio
async def test_control_normal_does_not_skip(fake_redis):
    """SystemState 미설정 (NORMAL) → 정상 발행."""
    from prime_jennie_runtime.control.state import SystemState

    observer = CollectingObserver()
    comp = _make_components(
        fake_redis,
        observer,
        _default_scout_output(),
        _default_macro_output(gate="open", size=0.75),
        system_state=SystemState(fake_redis),
    )
    result = await run_slow_loop(
        comp,
        as_of_date=date(2026, 4, 16),
        as_of_dt=datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST),
        macro_run_id="macro_20260416_0800",
        scout_run_id="scout_20260416_0830",
    )
    assert result.skipped_reason is None
    assert len(result.sheets_published) == 3


# =====================================================================
# 6. Risk throttle — Redis 의 intraday:risk:level 이 final_pct 에 반영됨
# =====================================================================


@pytest.mark.asyncio
async def test_redis_risk_throttle_applies_to_final_pct(fake_redis):
    """fast_loop 가 적재한 WARNING(0.6) 이 시트 size.risk_multiplier 에 반영됨."""
    from prime_jennie_runtime.fast_loop.risk_throttle import REDIS_LEVEL_KEY
    from prime_jennie_runtime.slow_loop.strategy.risk_throttle import (
        RedisRiskThrottleSnapshot,
    )

    # fast_loop 가 WARNING 을 적재한 상태 시뮬레이션
    await fake_redis.set(REDIS_LEVEL_KEY, "WARNING")

    observer = CollectingObserver()
    risk = RedisRiskThrottleSnapshot(fake_redis)
    comp = _make_components(
        fake_redis,
        observer,
        _default_scout_output(),
        _default_macro_output(gate="open", size=1.0),
        risk_throttle=risk,
    )
    result = await run_slow_loop(
        comp,
        as_of_date=date(2026, 4, 16),
        as_of_dt=datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST),
        macro_run_id="macro_20260416_0800",
        scout_run_id="scout_20260416_0830",
    )
    assert result.skipped_reason is None
    # refresh 가 pipeline 진입에서 호출되어 WARNING (0.6) 로 갱신
    assert risk.current_level == "WARNING"
    assert risk.current_multiplier() == pytest.approx(0.6, abs=1e-9)
    # 시트도 발행되었고 risk_mult 가 반영되었어야 함 — observer event 의 final_pct 확인
    pub_events = [e for e in observer.events if e.name == "pj.strategy.sheet_published"]
    assert pub_events
    # SECTOR_MOMENTUM base_pct=0.05 * macro=1.0 * risk=0.6 = 0.030
    for ev in pub_events:
        final_pct = ev.metadata.get("final_pct")
        assert final_pct == pytest.approx(0.03, abs=1e-9), (
            f"expected 0.03 with WARNING risk, got {final_pct}"
        )
