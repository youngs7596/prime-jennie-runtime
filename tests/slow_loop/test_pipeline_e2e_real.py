"""Slow Loop E2E — real NewsPipelineScoutFeeder + real ScreeningToolAdapter(subprocess).

stub → real 드롭인 교체를 실제 파이프라인 전 구간에서 검증.

- News: ``InMemoryEventRepo`` + ``NewsPipelineScoutFeeder`` 로 ticker별 평균 score 산출
- Screening: ``ScreeningToolAdapter()`` 로 Scout 코드가 자식 프로세스에서
  실제로 ``exec`` 되고 list[ScreeningCandidate] 를 돌려주는지 확인

LLM 응답(Scout/Macro structured output)만 StubChatModel 로 고정. 그 외는 모두 실 구현.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

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
from prime_jennie_runtime.news_pipeline_kor import (
    InMemoryEventRepo,
    NewsEvent,
    NewsPipelineScoutFeeder,
)
from prime_jennie_runtime.position_sheet.schema import KST
from prime_jennie_runtime.screening_executor.adapter import ScreeningToolAdapter
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
    StubSectorMomentumFeeder,
    StubUniverseFeeder,
)
from prime_jennie_runtime.slow_loop.scout.role import ScoutRole
from prime_jennie_runtime.slow_loop.scout.schemas import ScoutOutput
from prime_jennie_runtime.slow_loop.scout.screening_stub import ScreeningInvoker
from prime_jennie_runtime.slow_loop.strategy.engine import StrategyEngine
from prime_jennie_runtime.slow_loop.strategy.policy import load_policy
from prime_jennie_runtime.slow_loop.strategy.publisher import PositionSheetPublisher
from prime_jennie_runtime.slow_loop.strategy.risk_throttle import NoOpRiskThrottle

from ._stubs import StubChatModel

# Scout 코드: context.universe 와 context.news_events 를 읽고 positive_events 가
# 있는 ticker 를 후보로 만든다. 실 subprocess 에서 exec 되어야 통과.
REAL_SCREEN_CODE = """
def screen(market_data, context):
    universe = context.get("universe", [])
    news = context.get("news_events", {})
    out = []
    for ticker in universe:
        entry = news.get(ticker, {})
        if entry.get("risk_events"):
            continue
        positive = entry.get("positive_events", [])
        if not positive:
            continue
        high_events = entry.get("events_by_impact", {}).get("high", {})
        earnings_cnt = high_events.get("earnings", 0)
        conviction = 0.6 + 0.1 * min(earnings_cnt, 3)
        out.append({
            "ticker": ticker,
            "strategy_tag": "SECTOR_MOMENTUM",
            "conviction": min(conviction, 1.0),
            "entry_hint": {"trigger": "market"},
            "factors": {"high_earnings": float(earnings_cnt)},
            "notes": "real-e2e",
        })
    return out
"""


def _scout_output_with(code: str) -> ScoutOutput:
    return ScoutOutput(
        screening_code=code,
        hypothesis="뉴스 감성 상위 ticker 모멘텀",
        expected_candidates=2,
        factor_weights={"news": 1.0},
        strategy_tags_used=["SECTOR_MOMENTUM"],
        fallback_strategy="skip_today",
        estimated_runtime_seconds=5.0,
    )


def _macro_output_open() -> MacroGateOutput:
    return MacroGateOutput(
        gate="open",
        size_multiplier=1.0,
        reasoning="test",
        top_risks=[],
        confidence="medium",
        news_digest_ref="wsj_stub_0000",
    )


def _event(
    ticker: str,
    article_id: str,
    when: datetime,
    *,
    event_type: str = "other",
    impact_level: str = "medium",
    sentiment_score: float = 0.0,
) -> NewsEvent:
    sentiment = (
        "positive"
        if sentiment_score > 0.2
        else ("negative" if sentiment_score < -0.2 else "neutral")
    )
    return NewsEvent(
        article_id=article_id,
        ticker=ticker,
        published_at=when,
        event_type=event_type,  # type: ignore[arg-type]
        impact_level=impact_level,  # type: ignore[arg-type]
        sentiment=sentiment,  # type: ignore[arg-type]
        sentiment_score=sentiment_score,
        time_horizon="short",
        keywords=[],
        sector_tags=[],
        financial_signals=[],
        confidence=0.5,
        model="test",
        analyzed_at=when,
    )


def _make_real_components(
    fake_redis,
    observer,
    scout_out: ScoutOutput,
    macro_out: MacroGateOutput,
    sentiment_repo: InMemoryEventRepo,
    screening: ScreeningInvoker,
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
        news=NewsPipelineScoutFeeder(event_repo=sentiment_repo),
        sector=StubSectorMomentumFeeder(),
        market=StubMarketSummaryFeeder(),
    )
    macro_builder = MacroContextBuilder(
        wsj=StubWsjDigestFeeder(),
        market=StubMarketSnapshotFeeder(),
        kor=StubKorMacroNewsFeeder(),
    )

    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
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
    )


# =====================================================================
# 1. Real feeder + real subprocess adapter — happy path
# =====================================================================


@pytest.mark.asyncio
async def test_real_feeder_and_subprocess_adapter_publish_sheets(fake_redis):
    """News repo에 감성 점수 투입 → real feeder가 score > 0.3 ticker 산출 →
    subprocess executor가 그걸 읽어서 후보 반환 → strategy engine이 시트 발행."""
    as_of = date(2026, 4, 16)
    t = datetime.combine(as_of, datetime.min.time()).replace(hour=12)

    repo = InMemoryEventRepo()
    # 005930, 000660 → high earnings (positive_events 존재, 발행 대상)
    await repo.upsert(
        _event("005930", "a1", t, event_type="earnings", impact_level="high", sentiment_score=0.8)
    )
    await repo.upsert(
        _event(
            "005930",
            "a2",
            t - timedelta(hours=2),
            event_type="earnings",
            impact_level="high",
            sentiment_score=0.6,
        )
    )
    await repo.upsert(
        _event("000660", "b1", t, event_type="earnings", impact_level="high", sentiment_score=0.5)
    )
    # 035420 → impact=medium 이라 positive_events 리스트에 안 잡힘 (통과 X)
    await repo.upsert(
        _event("035420", "c1", t, event_type="earnings", impact_level="medium", sentiment_score=0.1)
    )
    # 207940, 005380 → 기사 0건 (feeder가 빈 entry 로 채움, 통과 X)

    observer = CollectingObserver()
    adapter = ScreeningToolAdapter(timeout_s=30.0)
    assert isinstance(adapter, ScreeningInvoker)  # Protocol 준수

    comp = _make_real_components(
        fake_redis,
        observer,
        _scout_output_with(REAL_SCREEN_CODE),
        _macro_output_open(),
        repo,
        adapter,
    )

    result = await run_slow_loop(
        comp,
        as_of_date=as_of,
        as_of_dt=datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST),
        macro_run_id="macro_20260416_0800",
        scout_run_id="scout_20260416_0830",
    )

    assert result.skipped_reason is None, f"unexpected skip: {result.skipped_reason}"
    assert len(result.sheets_published) == 2
    assert result.validation is not None
    published_tickers = {c.ticker for c in result.validation.candidates}
    assert published_tickers == {"005930", "000660"}

    length = await fake_redis.xlen(STREAM_POSITION_SHEETS)
    assert length == 2

    names = {e.name for e in observer.events}
    assert "pj.scout.code_generated" in names
    assert "pj.strategy.sheet_published" in names


# =====================================================================
# 2. Real feeder 빈 repo → real adapter 정상 실행, no_candidates
# =====================================================================


@pytest.mark.asyncio
async def test_real_feeder_empty_repo_subprocess_still_runs(fake_redis):
    """SentimentRepo가 비어도 NewsPipelineScoutFeeder 는 각 ticker에 score=0 entry를 채운다.
    Scout 코드가 score >= 0.3 필터링이므로 결과는 0건 → no_candidates 로 skipped.
    핵심은 real adapter가 JSON 직렬화 실패 없이 정상 spawn/return 한다는 점."""
    observer = CollectingObserver()
    repo = InMemoryEventRepo()  # 비어있음
    adapter = ScreeningToolAdapter(timeout_s=30.0)

    comp = _make_real_components(
        fake_redis,
        observer,
        _scout_output_with(REAL_SCREEN_CODE),
        _macro_output_open(),
        repo,
        adapter,
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
# 3. Real adapter가 context 직렬화를 견딘다 (date/datetime 포함)
# =====================================================================


@pytest.mark.asyncio
async def test_real_adapter_handles_json_unsafe_context_via_pipeline(fake_redis):
    """pipeline.py 가 screening_context를 JSON-safe 로 변환하는지 회귀 검증.
    이 변환이 깨지면 subprocess 에서 ``json.dumps`` 가 TypeError 를 내고
    ``ScreeningResult(ok=False, error='subprocess_error')`` → 빈 list 로 no_candidates.
    아래 테스트 통과는 변환이 올바르다는 간접 증거."""
    as_of = date(2026, 4, 16)
    t = datetime.combine(as_of, datetime.min.time()).replace(hour=12)

    repo = InMemoryEventRepo()
    await repo.upsert(
        _event("005930", "a1", t, event_type="earnings", impact_level="high", sentiment_score=0.9)
    )

    observer = CollectingObserver()
    adapter = ScreeningToolAdapter(timeout_s=30.0)

    comp = _make_real_components(
        fake_redis,
        observer,
        _scout_output_with(REAL_SCREEN_CODE),
        _macro_output_open(),
        repo,
        adapter,
    )

    result = await run_slow_loop(
        comp,
        as_of_date=as_of,
        as_of_dt=datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST),
        macro_run_id="macro_20260416_0800",
        scout_run_id="scout_20260416_0830",
    )

    assert result.skipped_reason is None
    assert len(result.sheets_published) == 1
