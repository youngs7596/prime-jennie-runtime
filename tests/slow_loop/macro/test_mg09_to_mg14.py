"""MACRO_GATE_SPEC §9 MG09~MG14 통합/단위 테스트.

기존 MG01~MG08, MG15~MG22 는 다른 파일에 분산. 본 파일은 누락된 케이스 전용:

- MG09: LLM 호출 3회 실패 → role 실패 + 직전 attempt 사유 누적 + previous_attempts 주입 검증
- MG10: WSJ digest 부재 (digest_id="unavailable") → 실행 계속, news_digest_ref 통과
- MG11: top_risks 6개 반환 → Pydantic 검증 실패 (재시도 트리거됨)
- MG12: reasoning 800자 → BeforeValidator truncate 후 정상 반영
- MG13: Redis pub 발행 후 MacroStateStore 갱신 확인 (state_store 통합)
- MG14: auto 트리거 (KOSPI -3.2%) → trigger_watcher 발화 → invoker 호출
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import fakeredis.aioredis
import pytest
from minyoung_mah import CollectingObserver

from prime_jennie_runtime.slow_loop.macro.context_builder import MacroContextBuilder
from prime_jennie_runtime.slow_loop.macro.feeders.stub import (
    StubKorMacroNewsFeeder,
    StubMarketSnapshotFeeder,
    StubWsjDigestFeeder,
)
from prime_jennie_runtime.slow_loop.macro.post_processor import run_post_processing
from prime_jennie_runtime.slow_loop.macro.schemas import (
    IndexPoint,
    MacroAttemptHint,
    MacroContext,
    MacroGateOutput,
    MarketSnapshot,
    RiskItem,
)
from prime_jennie_runtime.slow_loop.macro.state_store import (
    MacroCurrentState,
    MacroStateStore,
)
from prime_jennie_runtime.slow_loop.macro.trigger_watcher import (
    DEBOUNCE_KEY_PREFIX,
    KOSPI_REASON,
    MACRO_SNAPSHOT_KEY_PREFIX,
    TriggerWatcher,
)


def _normal_snapshot() -> MarketSnapshot:
    ip = IndexPoint(close=2800.0, change_pct=0.005)
    return MarketSnapshot(
        as_of=datetime.now(UTC),
        kospi=ip,
        kosdaq=ip,
        sp500=ip,
        nasdaq=ip,
        nikkei=ip,
        hsi=ip,
        usd_krw=1350.0,
        usd_krw_change_pct=0.002,
        usd_jpy=150.0,
        crude_oil=80.0,
        crude_oil_change_pct=0.0,
        gold=2300.0,
        gold_change_pct=0.0,
        vix=18.0,
        kospi_20d_vol=0.15,
        kospi_60d_vol=0.14,
        major_sector_drops=[],
    )


# =====================================================================
# MG09 — LLM 3회 실패: previous_attempts 누적
# =====================================================================


@pytest.mark.asyncio
async def test_mg09_retry_helper_accumulates_previous_attempts():
    """_run_macro_with_retry 가 매 시도마다 ctx.previous_attempts 를 갱신하고
    마지막에 RuntimeError 를 던지는지 확인 (직접 호출).

    Orchestrator 는 항상 실패하는 stub. 매 호출마다 ctx 의 previous_attempts
    길이가 1씩 늘어야 함.
    """
    from prime_jennie_runtime.slow_loop.pipeline import _run_macro_with_retry

    captured_attempts: list[int] = []  # 각 호출 시 previous_attempts 길이

    class _FailingOrch:
        async def run_pipeline(self, pipeline: Any, user_request: str = "") -> Any:
            # pipeline 의 input_mapping → InvocationContext 에서 macro_context 추출
            step = pipeline.steps[0]
            inv = step.input_mapping(None)
            ctx = inv.metadata.get("macro_context")
            captured_attempts.append(len(ctx.previous_attempts))
            raise RuntimeError("simulated LLM parse failure")

    obs = CollectingObserver()
    ctx = MacroContext(
        as_of=datetime.now(UTC),
        trigger_reason="test",
        market_snapshot=_normal_snapshot(),
        wsj_digest_id="wsj_test",
    )

    with pytest.raises(RuntimeError, match="macro_gate failed after 3"):
        await _run_macro_with_retry(_FailingOrch(), ctx, obs, max_attempts=3)

    # 3회 호출 + 각 호출 시 previous_attempts 누적
    assert captured_attempts == [0, 1, 2]

    # 이벤트: retry 2회 + failed 1회
    names = [e.name for e in obs.events]
    assert names.count("pj.macro_gate.retry") == 3
    assert names.count("pj.macro_gate.failed") == 1


@pytest.mark.asyncio
async def test_mg09_attempt_hint_in_prompt():
    """MacroAttemptHint 가 user prompt 의 retry_section 으로 나가는지 확인."""
    from prime_jennie_runtime.slow_loop.macro.prompts import build_user_prompt

    ctx = MacroContext(
        as_of=datetime.now(UTC),
        trigger_reason="test",
        market_snapshot=_normal_snapshot(),
        wsj_digest_id="wsj_test",
        previous_attempts=[
            MacroAttemptHint(
                attempt_no=1,
                error="role_invocation_failed",
                details="top_risks length 6 > 5",
            ),
            MacroAttemptHint(
                attempt_no=2,
                error="role_invocation_failed",
                details="reasoning > 500 chars",
            ),
        ],
    )
    msg = build_user_prompt(ctx)
    assert "직전 시도 실패" in msg
    assert "시도 #1" in msg
    assert "시도 #2" in msg
    assert "top_risks length 6 > 5" in msg
    assert "체크리스트" in msg


def test_prompt_renders_vkospi_and_market_flows():
    """VKOSPI 실값 + 시장전체 수급(외국인/기관/연기금) 이 게이트 프롬프트에 노출되는지.
    값이면 부호+콤마, 미수집(None)이면 N/A (2026-06-29 맥락 입력 연결)."""
    from prime_jennie_runtime.slow_loop.macro.prompts import build_user_prompt

    snap = _normal_snapshot().model_copy(
        update={
            "vkospi": 92.71,
            "market_foreign_net": -77332.0,
            "market_institution_net": 29327.0,
            "market_pension_net": 581.0,
        }
    )
    ctx = MacroContext(
        as_of=datetime.now(UTC),
        trigger_reason="test",
        market_snapshot=snap,
        wsj_digest_id="wsj_test",
    )
    msg = build_user_prompt(ctx)
    assert "VKOSPI: 92.71" in msg
    assert "시장 수급" in msg
    assert "외국인: -77,332" in msg
    assert "기관계: +29,327" in msg
    assert "연기금: +581" in msg

    # 미수집(None) → N/A 로 graceful 표시
    ctx_na = MacroContext(
        as_of=datetime.now(UTC),
        trigger_reason="test",
        market_snapshot=_normal_snapshot(),
        wsj_digest_id="wsj_test",
    )
    msg_na = build_user_prompt(ctx_na)
    assert "외국인: N/A" in msg_na


# =====================================================================
# MG10 — WSJ digest 부재: news_digest_ref="unavailable" 로 실행 계속
# =====================================================================


@pytest.mark.asyncio
async def test_mg10_wsj_digest_unavailable_does_not_block():
    """digest_id='unavailable' 이어도 build_user_prompt / post_processing 정상."""
    from prime_jennie_runtime.slow_loop.macro.prompts import build_user_prompt

    ctx = MacroContext(
        as_of=datetime.now(UTC),
        trigger_reason="test",
        market_snapshot=_normal_snapshot(),
        wsj_digest_id="unavailable",
        wsj_macro_summary="",
        wsj_headlines_formatted="",
    )
    msg = build_user_prompt(ctx)
    assert "unavailable" in msg

    # post_processing 도 raw output 의 news_digest_ref 를 그대로 통과시켜야 함
    raw = MacroGateOutput(
        gate="open",
        size_multiplier=0.75,
        reasoning="ok",
        top_risks=[],
        confidence="medium",
        news_digest_ref="unavailable",
    )
    result = await run_post_processing(raw, _normal_snapshot(), [], CollectingObserver())
    assert result.output.news_digest_ref == "unavailable"
    assert result.output.gate == "open"


# =====================================================================
# MG11 — top_risks 6개 반환 → validator 실패
# =====================================================================


def test_mg11_top_risks_overflow_fails_pydantic():
    """6개 입력 시 Pydantic max_length 위반 — ValidationError."""
    from pydantic import ValidationError

    six_risks = [
        RiskItem(category="other", description=f"risk{i}", severity="low") for i in range(6)
    ]
    with pytest.raises(ValidationError):
        MacroGateOutput(
            gate="open",
            size_multiplier=0.75,
            reasoning="ok",
            top_risks=six_risks,
            confidence="medium",
            news_digest_ref="wsj_test",
        )


def test_mg11_top_risks_exactly_five_ok():
    """경계값 5개는 OK."""
    five_risks = [
        RiskItem(category="other", description=f"risk{i}", severity="low") for i in range(5)
    ]
    out = MacroGateOutput(
        gate="open",
        size_multiplier=0.75,
        reasoning="ok",
        top_risks=five_risks,
        confidence="medium",
        news_digest_ref="wsj_test",
    )
    assert len(out.top_risks) == 5


# =====================================================================
# MG12 — reasoning 800자: BeforeValidator truncate 후 통과
# =====================================================================


def test_mg12_reasoning_overflow_truncates_to_2000():
    """현재 스펙은 BeforeValidator 가 2000 자로 자른다 (Field max_length 가 아닌 truncate).

    spec 의 500자 권장은 system_prompt 차원 가이드. 실제 schema 차단선은 2000.
    """
    long_reasoning = "가" * 3000
    out = MacroGateOutput(
        gate="open",
        size_multiplier=0.75,
        reasoning=long_reasoning,
        top_risks=[],
        confidence="medium",
        news_digest_ref="wsj_test",
    )
    assert len(out.reasoning) == 2000
    assert out.gate == "open"  # truncate 후에도 gate 보존


# =====================================================================
# MG13 — Redis pub: MacroStateStore.set 후 get/is_stale 검증
# =====================================================================


@pytest.mark.asyncio
async def test_mg13_state_store_round_trip():
    """run_slow_loop 가 호출하는 state_store.set 의 round-trip + stale 갱신 확인."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    store = MacroStateStore(redis)

    now = datetime.now(UTC)
    state = MacroCurrentState(
        macro_run_id="mr_test_001",
        gate="open",
        size_multiplier=0.75,
        generated_at=now,
    )
    await store.set(state)

    # 직접 raw key 확인 (저장됐는지)
    keys = await redis.keys("*macro*")
    assert keys, "macro state key not set in redis"

    # is_stale: 방금 저장했으므로 False
    assert await store.is_stale(now=now) is False
    # 25시간 뒤에는 stale
    far_future = now + timedelta(hours=25)
    assert await store.is_stale(now=far_future) is True


# =====================================================================
# MG14 — auto 트리거 (KOSPI -3.2%) → TriggerWatcher 발화
# =====================================================================


@pytest.mark.asyncio
async def test_mg14_auto_trigger_kospi_drop_invokes_with_reason():
    """KOSPI -3.2% snapshot → KOSPI_REASON 으로 invoker 호출."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    today = date(2026, 4, 16)
    key = f"{MACRO_SNAPSHOT_KEY_PREFIX}{today.isoformat()}"
    await redis.set(key, json.dumps({"kospi_change_pct": -3.2, "vix": 22.0, "usd_krw": 1350.0}))

    invoked: list[str] = []

    async def _invoker(reason: str) -> None:
        invoked.append(reason)

    class _NoopObs:
        async def emit(self, event: Any) -> None:  # noqa: D401
            return None

    watcher = TriggerWatcher(
        redis_client=redis,
        invoker=_invoker,
        observer=_NoopObs(),
        clock=lambda: datetime(2026, 4, 16, 10, 0),
    )
    fired = await watcher.process_once()
    assert any(s.reason == KOSPI_REASON for s in fired)
    assert invoked == [KOSPI_REASON]

    # 같은 reason 으로 30분 내 재호출 시 debounce — invoker 미호출
    fired2 = await watcher.process_once()
    assert fired2 == []
    assert invoked == [KOSPI_REASON]
    # debounce key 가 redis 에 set 됐는지 확인
    assert await redis.exists(f"{DEBOUNCE_KEY_PREFIX}{KOSPI_REASON}")


# =====================================================================
# 보조: MacroContextBuilder 가 새 필드 (previous_attempts, geopolitical_count) 를
# 디폴트로 채우는지 확인
# =====================================================================


@pytest.mark.asyncio
async def test_macro_context_builder_defaults_previous_attempts_empty():
    builder = MacroContextBuilder(
        wsj=StubWsjDigestFeeder(),
        market=StubMarketSnapshotFeeder(),
        kor=StubKorMacroNewsFeeder(),
    )
    ctx = await builder.build(datetime.now(UTC))
    assert ctx.previous_attempts == []
    assert ctx.market_snapshot.high_impact_geopolitical_count == 0
    assert ctx.market_snapshot.kospi_turnover_ratio is None
