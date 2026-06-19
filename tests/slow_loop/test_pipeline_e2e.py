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
from prime_jennie_runtime.screening_executor.schemas import ScreeningResult
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
from prime_jennie_runtime.slow_loop.scout.deterministic_scout import DeterministicScoutResult
from prime_jennie_runtime.slow_loop.scout.feeders.stub import (
    StubMarketSummaryFeeder,
    StubNewsEventFeeder,
    StubSectorMomentumFeeder,
    StubUniverseFeeder,
)
from prime_jennie_runtime.slow_loop.scout.schemas import (
    EntryHint,
    ScoutOutput,
    ScreeningCandidate,
)
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


def _default_candidates() -> list[ScreeningCandidate]:
    """E2E 기본 후보 3종 — 구 ScreeningToolAdapterStub 의 고정 후보를 인라인.

    정상 경로 테스트가 "3 시트 발행" 을 기대하므로 3종 유지.
    """
    return [
        ScreeningCandidate(
            ticker="005930",
            strategy_tag="SECTOR_MOMENTUM",
            conviction=0.72,
            entry_hint=EntryHint(trigger="limit", price_hint=71200.0),
            factors={"momentum_5d": 0.034, "news_score": 0.3},
            notes="반도체 모멘텀 (stub)",
        ),
        ScreeningCandidate(
            ticker="000660",
            strategy_tag="SECTOR_MOMENTUM",
            conviction=0.65,
            entry_hint=EntryHint(trigger="limit", price_hint=135000.0),
            factors={"momentum_5d": 0.028, "news_score": 0.2},
            notes="하이닉스 모멘텀 (stub)",
        ),
        ScreeningCandidate(
            ticker="207940",
            strategy_tag="EARNINGS_DRIFT",
            conviction=0.58,
            entry_hint=EntryHint(trigger="market"),
            factors={"eps_revision": 0.06, "news_score": 0.1},
            notes="삼성바이오 실적 (stub)",
        ),
    ]


def _fake_scout_runner(candidates: list[ScreeningCandidate]):
    """결정론 scout(run_deterministic_scout) 자리에 끼우는 fake — canned 후보 반환.

    포팅(2026-05-22 Phase 1 a) 후 pipeline 은 comp.scout_runner 를 호출한다. E2E 는
    DB 의존 enrichment 대신 이 fake 로 후보를 주입 (run_deterministic_scout 와 동일
    kwargs 시그니처). 후보 검증·시트 발행 등 하류 로직은 그대로 검증된다.
    """

    async def _runner(*, db_engine, scout_ctx, as_of, as_of_dt, scout_run_id):
        return DeterministicScoutResult(
            scout_out=_default_scout_output(),
            result=ScreeningResult(ok=True, candidates=list(candidates)),
        )

    return _runner


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
        role_registry=RoleRegistry.of(MacroGateRole()),
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

    # 결정론 scout 포팅(2026-05-22) 후 pipeline 은 comp.scout_runner 를 호출한다.
    # canned 후보를 그대로 돌려주는 fake runner 를 주입 (DB 의존 enrichment 우회).
    candidates = _default_candidates() if screening_candidates is None else screening_candidates
    scout_runner = _fake_scout_runner(candidates)

    engine = StrategyEngine(load_policy(), risk_throttle or NoOpRiskThrottle())
    publisher = PositionSheetPublisher(fake_redis)
    state_store = MacroStateStore(fake_redis)

    return SlowLoopComponents(
        orchestrator=orch,
        scout_builder=scout_builder,
        macro_builder=macro_builder,
        engine=engine,
        publisher=publisher,
        state_store=state_store,
        observer=observer,
        system_state=system_state,
        scout_runner=scout_runner,
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
# 2. Macro closed (paper shadow) — Scout 는 돌려 점수·후보 영속, 매매는 0
# =====================================================================


@pytest.mark.asyncio
async def test_macro_closed_shadow_records_scout_no_trading(fake_redis):
    """게이트가 닫혀도 Scout 를 돌려 점수·후보를 남기고, 매매 핸드오프만 차단한다.

    종전엔 닫히면 Scout 를 통째로 생략해 닫힌 날 데이터가 사라졌다. paper alpha
    실험에선 그 데이터가 "게이트가 옳았나"를 측정하는 유일한 근거라, STOP/PAUSE 와
    동일하게 "분석·영속은 수행, fast_loop 핸드오프만 차단" 으로 바꿨다.
    """
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
    # 매매는 안 했지만 Scout 는 돌았다 — shadow.
    assert result.skipped_reason == "macro_closed_shadow"
    assert result.sheets_published == []
    assert result.scout_output is not None  # Scout 실행됨

    # fast_loop 핸드오프(시트 stream) 0건 — 닫힌 날 매매 없음.
    length = await fake_redis.xlen(STREAM_POSITION_SHEETS)
    assert length == 0

    names = {e.name for e in observer.events}
    assert "pj.macro.gate_closed" in names
    assert "pj.slow_loop.skipped_macro_closed" in names
    assert "pj.scout.code_generated" in names  # Scout 가 돌아 점수·후보 영속


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
# 5. control.state — STOP/PAUSE 시 fast_loop stream emit 만 차단
#    (게이트가 publish 직전으로 이동 — Macro/Scout 분석·DB영속은 정상 수행)
# =====================================================================


@pytest.mark.asyncio
async def test_control_stopped_runs_analysis_but_skips_stream_emit(fake_redis):
    """SystemState.stopped → Macro/Scout 분석은 수행, fast_loop stream emit 만 skip.

    게이트가 run_slow_loop 최상단에서 publish 직전으로 이동한 결과:
    STOP 중에도 macro/scout 가 돌아 관측·백테스트 데이터가 남고, fast_loop
    핸드오프(v3:position_sheets stream)만 차단된다 → 진입 0.
    """
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
    # fast_loop 핸드오프 차단 — stream 에 0건, 진입 0
    assert result.sheets_published == []
    assert await fake_redis.xlen(STREAM_POSITION_SHEETS) == 0

    names = {e.name for e in observer.events}
    # Macro/Scout 분석은 STOP 중에도 수행됨 (게이트가 더 이상 최상단이 아님)
    assert "pj.scout.code_generated" in names
    assert "pj.slow_loop.publish_blocked_control" in names
    # 시트는 build + DB 영속까지 됐으나 stream emit 은 skip
    assert "pj.strategy.sheet_persisted_no_emit" in names
    assert "pj.strategy.sheet_published" not in names


@pytest.mark.asyncio
async def test_control_paused_runs_analysis_but_skips_stream_emit(fake_redis):
    """SystemState.paused → STOP 과 동일하게 분석은 수행, stream emit 만 skip."""
    from prime_jennie_runtime.control.state import SystemState
    from prime_jennie_runtime.infra.redis_streams import STREAM_POSITION_SHEETS
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
    assert await fake_redis.xlen(STREAM_POSITION_SHEETS) == 0
    names = {e.name for e in observer.events}
    assert "pj.scout.code_generated" in names
    assert "pj.slow_loop.publish_blocked_control" in names


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


# =====================================================================
# 7. DLQ 송부 — hallucination_fail / engine_error / publisher_error
# =====================================================================


@pytest.mark.asyncio
async def test_dlq_on_scout_hallucination_fail(fake_redis):
    """Scout hallucination_fail (universe 밖 >=50%) 시 raw candidates 가 DLQ 로 가야 함."""
    import json as _json

    from prime_jennie_runtime.infra.redis_streams import STREAM_POSITION_SHEETS_DLQ

    observer = CollectingObserver()
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
        ScreeningCandidate(
            ticker="005930",
            strategy_tag="SECTOR_MOMENTUM",
            conviction=0.9,
            entry_hint=EntryHint(trigger="limit", price_hint=71200.0),
        ),
    ]

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

    # DLQ stream 에 1건 적재
    dlq_len = await fake_redis.xlen(STREAM_POSITION_SHEETS_DLQ)
    assert dlq_len == 1
    # dlq_reason == scout_hallucination_fail
    entries = await fake_redis.xrange(STREAM_POSITION_SHEETS_DLQ, count=1)
    _id, fields = entries[0]
    raw_payload = fields[b"payload"].decode()
    envelope = _json.loads(raw_payload)
    inner = _json.loads(envelope["payload"])
    assert inner["dlq_reason"] == "scout_hallucination_fail"
    assert inner["payload"]["scout_run_id"] == "scout_20260416_0830"

    names = {e.name for e in observer.events}
    assert "pj.slow_loop.dlq_sent" in names


@pytest.mark.asyncio
async def test_dlq_on_engine_build_sheet_error(fake_redis, monkeypatch):
    """engine.build_sheet 예외 시 raw candidate 가 DLQ 로 가야 함."""
    import json as _json

    from prime_jennie_runtime.infra.redis_streams import STREAM_POSITION_SHEETS_DLQ

    observer = CollectingObserver()
    valid_cands = [
        ScreeningCandidate(
            ticker="005930",
            strategy_tag="SECTOR_MOMENTUM",
            conviction=0.9,
            entry_hint=EntryHint(trigger="limit", price_hint=71200.0),
        ),
    ]
    comp = _make_components(
        fake_redis,
        observer,
        _default_scout_output(),
        _default_macro_output(gate="open", size=1.0),
        screening_candidates=valid_cands,
    )

    # engine.build_sheet_with_reason 을 일부러 예외내도록 monkeypatch
    async def _boom(cand, inputs):
        raise RuntimeError("schema fail")

    monkeypatch.setattr(comp.engine, "build_sheet_with_reason", _boom)

    result = await run_slow_loop(
        comp,
        as_of_date=date(2026, 4, 16),
        as_of_dt=datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST),
        macro_run_id="macro_20260416_0800",
        scout_run_id="scout_20260416_0830",
    )
    assert result.sheets_published == []
    assert "005930" in result.sheets_rejected

    dlq_len = await fake_redis.xlen(STREAM_POSITION_SHEETS_DLQ)
    assert dlq_len == 1
    entries = await fake_redis.xrange(STREAM_POSITION_SHEETS_DLQ, count=1)
    _id, fields = entries[0]
    envelope = _json.loads(fields[b"payload"].decode())
    assert "schema fail" in envelope["error"]
    inner = _json.loads(envelope["payload"])
    assert inner["dlq_reason"] == "engine_build_sheet_error"
    assert inner["payload"]["candidate"]["ticker"] == "005930"


@pytest.mark.asyncio
async def test_dlq_on_publisher_publish_failure(fake_redis, monkeypatch):
    """publisher.publish 예외 시 pipeline 도 sheet 컨텍스트를 DLQ 로 보냄."""
    import json as _json

    from prime_jennie_runtime.infra.redis_streams import STREAM_POSITION_SHEETS_DLQ

    observer = CollectingObserver()
    valid_cands = [
        ScreeningCandidate(
            ticker="005930",
            strategy_tag="SECTOR_MOMENTUM",
            conviction=0.9,
            entry_hint=EntryHint(trigger="limit", price_hint=71200.0),
        ),
    ]
    comp = _make_components(
        fake_redis,
        observer,
        _default_scout_output(),
        _default_macro_output(gate="open", size=1.0),
        screening_candidates=valid_cands,
    )

    # publish 가 DB upsert 단계에서 예외내도록 — DB upsert 후 stream 발행 전.
    # _persist_sheet 가 RuntimeError 던지면 publisher 가 raw sheet DLQ 호출 없이 raise.
    async def _persist_fail(sheet):
        raise RuntimeError("db upsert exploded")

    monkeypatch.setattr(comp.publisher, "_persist_sheet", _persist_fail)

    result = await run_slow_loop(
        comp,
        as_of_date=date(2026, 4, 16),
        as_of_dt=datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST),
        macro_run_id="macro_20260416_0800",
        scout_run_id="scout_20260416_0830",
    )
    assert result.sheets_published == []
    assert "005930" in result.sheets_rejected

    # pipeline 의 DLQ 송부 1건 — publisher_error
    dlq_len = await fake_redis.xlen(STREAM_POSITION_SHEETS_DLQ)
    assert dlq_len == 1
    entries = await fake_redis.xrange(STREAM_POSITION_SHEETS_DLQ, count=1)
    _id, fields = entries[0]
    envelope = _json.loads(fields[b"payload"].decode())
    inner = _json.loads(envelope["payload"])
    assert inner["dlq_reason"] == "publisher_error"
    assert "db upsert exploded" in envelope["error"]
