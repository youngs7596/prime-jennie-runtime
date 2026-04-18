"""Slow Loop Pipeline 조립.

Phase 0 design §4. 구조:
  1. Macro Gate role (fast path)
  2. Macro 후처리 (결정론) — auto_override, discretize, 이벤트
  3. macro:current_state 저장 + stale 체크
  4. Scout role (fast path)
  5. Screening Executor 호출 (Track D stub) → candidates
  6. Scout 결과 검증 (validate_candidates)
  7. Strategy Engine.build_sheet × N
  8. PositionSheetPublisher.publish

StaticPipeline이 LLM 두 번만 호출한다 (Scout, Macro). 나머지는 파이프라인 밖
결정론 후처리. minyoung-mah에 없는 조건부 분기/retry를 local workaround로.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from minyoung_mah import (
    InvocationContext,
    Observer,
    Orchestrator,
    PipelineStep,
    StaticPipeline,
)

from prime_jennie_runtime.infra.observer_impl import pj_event
from prime_jennie_runtime.position_sheet.schema import MacroStateSnapshot

from .macro.context_builder import MacroContextBuilder
from .macro.post_processor import MacroPostResult, run_post_processing
from .macro.schemas import MacroGateOutput, RecentMacroRun
from .macro.state_store import MacroCurrentState, MacroStateStore
from .persistence import (
    persist_macro_run,
    persist_scout_run,
    persist_screening_candidates,
    update_candidate_promotion,
)
from .scout.code_hasher import compute_code_hash
from .scout.context_builder import ScoutContextBuilder
from .scout.schemas import (
    MacroStateForScout,
    ScoutOutput,
    ScoutRunSummary,
    ScreeningCandidate,
)
from .scout.screening_stub import ScreeningInvoker
from .scout.validators import ScoutValidationResult, validate_candidates
from .strategy.engine import StrategyEngine, StrategyEngineInputs
from .strategy.publisher import PositionSheetPublisher

logger = logging.getLogger(__name__)


# =====================================================================
# 결과 타입
# =====================================================================


@dataclass(frozen=True)
class SlowLoopResult:
    """한 번의 slow loop 실행 결과."""

    macro_post: MacroPostResult | None = None
    scout_output: ScoutOutput | None = None
    validation: ScoutValidationResult | None = None
    sheets_published: list[str] = field(default_factory=list)
    sheets_rejected: list[str] = field(default_factory=list)
    skipped_reason: str | None = None  # None이면 정상 종료


# =====================================================================
# Role retry 헬퍼 (minyoung-mah 밖의 local workaround)
# =====================================================================


async def _run_pipeline_with_retry(
    orchestrator: Orchestrator,
    pipeline: StaticPipeline,
    observer: Observer,
    *,
    role_name: str,
    max_attempts: int = 3,
    user_request: str = "",
) -> Any:
    """파이프라인을 돌리되 실패 시 최대 3회 재시도.

    단일 role의 구조화 출력 파싱이 드물게 실패할 때를 위한 방어망.
    (에러 컨텍스트를 다음 프롬프트에 주입하는 기능은 Phase 2로 연기 — 필요 시
    harness 측 retry policy로 승격.)
    """
    last_error: str | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = await orchestrator.run_pipeline(pipeline, user_request=user_request)
            if result.completed:
                return result
            last_error = result.error or f"aborted at step {result.aborted_at}"
        except Exception as e:  # noqa: BLE001
            last_error = f"{type(e).__name__}: {e}"

        await observer.emit(
            pj_event(
                f"pj.{role_name}.retry",
                role=role_name,
                ok=False,
                attempt=attempt,
                error=last_error,
            )
        )

    await observer.emit(
        pj_event(
            f"pj.{role_name}.failed",
            role=role_name,
            ok=False,
            attempts=max_attempts,
            error=last_error,
        )
    )
    raise RuntimeError(f"{role_name} failed after {max_attempts} attempts: {last_error}")


# =====================================================================
# Slow Loop 조립
# =====================================================================


@dataclass
class SlowLoopComponents:
    """Slow Loop 실행에 필요한 모든 컴포넌트 묶음."""

    orchestrator: Orchestrator
    scout_builder: ScoutContextBuilder
    macro_builder: MacroContextBuilder
    screening: ScreeningInvoker
    engine: StrategyEngine
    publisher: PositionSheetPublisher
    state_store: MacroStateStore
    observer: Observer
    db_engine: Any = None  # AsyncEngine | None — macro_runs/scout_runs 기록용
    shadow_orchestrator: Any = None  # Orchestrator | None — Macro 를 DeepSeek 로 shadow 평가
    redis_client: Any = None  # aioredis.Redis | None — LLM stats 누적용 (llm:stats:{date}:{svc})


def _macro_pipeline(macro_ctx: Any) -> StaticPipeline:
    return StaticPipeline(
        steps=[
            PipelineStep(
                name="macro_gate",
                role="macro_gate",
                input_mapping=lambda _s: InvocationContext(
                    task_summary="daily macro gate",
                    user_request="",
                    metadata={"macro_context": macro_ctx},
                ),
            ),
        ],
    )


def _scout_pipeline(scout_ctx: Any) -> StaticPipeline:
    return StaticPipeline(
        steps=[
            PipelineStep(
                name="scout",
                role="scout",
                input_mapping=lambda _s: InvocationContext(
                    task_summary="daily scout run",
                    user_request="",
                    metadata={"scout_context": scout_ctx},
                ),
            ),
        ],
    )


async def run_slow_loop(
    comp: SlowLoopComponents,
    *,
    as_of_date: date,
    as_of_dt: datetime,
    macro_run_id: str,
    scout_run_id: str,
    recent_macro_runs: list[RecentMacroRun] | None = None,
    previous_scout_runs: list[ScoutRunSummary] | None = None,
    macro_trigger: str = "scheduled_0800",
    scout_trigger: str = "scheduled_0830",
) -> SlowLoopResult:
    """하루치 slow loop 실행.

    Returns:
        SlowLoopResult — 발행된 시트 ID, 거부된 ticker, skipped 이유 포함.
    """
    observer = comp.observer

    # --- 1. Macro phase ---
    macro_ctx = await comp.macro_builder.build(
        as_of=as_of_dt,
        recent_runs=recent_macro_runs,
        trigger_reason=macro_trigger,
    )

    async def _primary_macro():
        return await _run_pipeline_with_retry(
            comp.orchestrator,
            _macro_pipeline(macro_ctx),
            observer,
            role_name="macro_gate",
            user_request="daily macro gate",
        )

    async def _shadow_macro():
        if comp.shadow_orchestrator is None:
            return None
        import time

        t0 = time.monotonic()
        try:
            res = await _run_pipeline_with_retry(
                comp.shadow_orchestrator,
                _macro_pipeline(macro_ctx),
                observer,
                role_name="macro_gate_shadow",
                user_request="daily macro gate (shadow)",
            )
            # metadata (usage 포함 — minyoung-mah 0.1.2+) 를 함께 실어 cost 계산에 활용.
            shadow_meta: dict[str, Any] = {}
            try:
                step = res.state["macro_gate"]
                if step.outputs:
                    shadow_meta = dict(step.outputs[0].metadata or {})
            except Exception:
                shadow_meta = {}
            return {
                "result": res,
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "metadata": shadow_meta,
            }
        except Exception as e:
            logger.warning("macro shadow failed: %s", e)
            return {"error": f"{type(e).__name__}: {e}"}

    import asyncio

    macro_result, shadow_payload = await asyncio.gather(
        _primary_macro(), _shadow_macro(), return_exceptions=False
    )
    raw_macro: MacroGateOutput | None = macro_result.state["macro_gate"].payload_as(MacroGateOutput)
    if raw_macro is None:
        await observer.emit(pj_event("pj.macro.output_missing", role="macro_gate", ok=False))
        return SlowLoopResult(skipped_reason="macro_output_missing")

    post = await run_post_processing(
        raw_macro,
        macro_ctx.market_snapshot,
        recent_macro_runs or [],
        observer,
    )

    # DB 기록 (engine=None 이면 no-op). prompt_chars 는 비용 추정용.
    from .macro.prompts import MACRO_SYSTEM_PROMPT
    from .macro.prompts import build_user_prompt as _build_macro_user

    macro_prompt_chars = len(MACRO_SYSTEM_PROMPT) + len(_build_macro_user(macro_ctx))

    # Shadow 결과 구성 — DeepSeek 가 낸 동일 스키마 output 을 그대로 기록
    shadow_payload_for_db: dict[str, Any] | None = None
    if shadow_payload is not None and "error" not in shadow_payload:
        try:
            shadow_res = shadow_payload["result"]
            shadow_raw: MacroGateOutput | None = shadow_res.state["macro_gate"].payload_as(
                MacroGateOutput
            )
            if shadow_raw is not None:
                shadow_cost_est = None
                try:
                    from .persistence import _resolve_cost, _tier_model

                    shadow_model_name = _tier_model("shadow_reasoning")  # DeepSeek V3.2 flagship
                    # shadow_payload 에 metadata (usage 포함 가능) 가 실려 있으면 우선 사용.
                    shadow_meta = shadow_payload.get("metadata") or {}
                    shadow_out_chars = len(shadow_raw.reasoning or "") + sum(
                        len(r.description or "") for r in shadow_raw.top_risks
                    )
                    shadow_cost_est = _resolve_cost(
                        shadow_meta, shadow_model_name, macro_prompt_chars, shadow_out_chars
                    )
                    shadow_payload_for_db = {
                        "model_used": shadow_model_name,
                        "gate": shadow_raw.gate,
                        "size_multiplier": float(shadow_raw.size_multiplier),
                        "reasoning": shadow_raw.reasoning,
                        "confidence": shadow_raw.confidence,
                        "top_risks": [r.model_dump() for r in shadow_raw.top_risks],
                        "latency_ms": shadow_payload.get("duration_ms"),
                        "cost_usd_estimated": shadow_cost_est,
                        "next_review_hint": getattr(shadow_raw, "next_review_hint", None),
                    }
                except Exception:
                    logger.exception("macro shadow payload build failed")
        except Exception:
            logger.exception("macro shadow result extract failed")
    elif shadow_payload and "error" in shadow_payload:
        shadow_payload_for_db = {"error": shadow_payload["error"]}

    await persist_macro_run(
        comp.db_engine,
        macro_run_id=macro_run_id,
        generated_at=as_of_dt,
        trigger_reason=macro_trigger,
        post=post,
        macro_step_result=macro_result.state["macro_gate"],
        news_digest_ref=getattr(macro_ctx, "news_digest_ref", None),
        prompt_chars=macro_prompt_chars,
        shadow_result=shadow_payload_for_db,
        redis_client=comp.redis_client,
    )

    # 상태 저장
    await comp.state_store.set(
        MacroCurrentState(
            macro_run_id=macro_run_id,
            gate=post.output.gate,
            size_multiplier=post.output.size_multiplier,
            generated_at=as_of_dt,
        )
    )

    # stale 체크 — 방금 저장했으므로 아닐 것이지만 방어적
    if await comp.state_store.is_stale(now=as_of_dt):
        await observer.emit(pj_event("pj.macro.stale_detected", role="macro_gate", ok=False))
        return SlowLoopResult(macro_post=post, skipped_reason="stale_macro")

    # Macro gate closed → Scout 돌려도 어차피 engine이 전부 None 반환.
    # 비용 절감 차 Scout phase 생략.
    if post.output.gate == "closed":
        await observer.emit(
            pj_event(
                "pj.slow_loop.skipped_macro_closed",
                role="slow_loop",
                ok=True,
                triggers=list(post.closed_triggers),
            )
        )
        return SlowLoopResult(macro_post=post, skipped_reason="macro_closed")

    # --- 2. Scout phase ---
    macro_for_scout = MacroStateForScout(
        gate=post.output.gate,
        size_multiplier=post.output.size_multiplier,
        gate_run_id=macro_run_id,
        top_risks_summary="; ".join(r.description for r in post.output.top_risks),
    )
    scout_ctx = await comp.scout_builder.build(
        as_of=as_of_date,
        macro_state=macro_for_scout,
        previous_runs=previous_scout_runs,
        trigger_reason=scout_trigger,
    )
    scout_result = await _run_pipeline_with_retry(
        comp.orchestrator,
        _scout_pipeline(scout_ctx),
        observer,
        role_name="scout",
        user_request="daily scout run",
    )
    scout_out: ScoutOutput | None = scout_result.state["scout"].payload_as(ScoutOutput)
    if scout_out is None:
        await observer.emit(pj_event("pj.scout.output_missing", role="scout", ok=False))
        return SlowLoopResult(macro_post=post, skipped_reason="scout_output_missing")

    # DB 기록 (engine=None 이면 no-op). candidates_count 는 screening 후 갱신되지 않으므로
    # expected_candidates 로 대체.
    from .scout.prompts import SCOUT_SYSTEM_PROMPT
    from .scout.prompts import build_user_prompt as _build_scout_user

    scout_prompt_chars = len(SCOUT_SYSTEM_PROMPT) + len(_build_scout_user(scout_ctx))

    # context snapshot — 백테스트 재현용. Scout 코드 입력을 그대로 pickle-friendly
    # 형태로 scout_runs.context_snapshot_json 에 저장. universe 는 해시와 size 만
    # 남기고 full list 는 universe_hash 가 일치하는 외부 테이블(stock_masters 등)
    # 에서 복원 — full list 는 크고 변동 주기가 길어 매 run 중복 저장할 가치 낮음.
    import hashlib as _hashlib

    _universe_raw = ",".join(sorted(scout_ctx.universe))
    scout_context_snapshot = {
        "as_of": as_of_date.isoformat(),
        "trigger_reason": scout_trigger,
        "universe_size": len(scout_ctx.universe),
        "universe_hash": _hashlib.sha256(_universe_raw.encode("utf-8")).hexdigest(),
        "news_scores": {t: e.model_dump(mode="json") for t, e in scout_ctx.news_scores.items()},
        "sector_momentum": dict(scout_ctx.sector_momentum),
        "macro_size_multiplier": float(post.output.size_multiplier),
        "macro_gate": post.output.gate,
        "macro_run_id": macro_run_id,
    }
    await persist_scout_run(
        comp.db_engine,
        scout_run_id=scout_run_id,
        generated_at=as_of_dt,
        scout_out=scout_out,
        scout_step_result=scout_result.state["scout"],
        prompt_chars=scout_prompt_chars,
        context_snapshot=scout_context_snapshot,
        redis_client=comp.redis_client,
    )

    await observer.emit(
        pj_event(
            "pj.scout.code_generated",
            role="scout",
            ok=True,
            hypothesis=scout_out.hypothesis,
            expected_candidates=scout_out.expected_candidates,
        )
    )

    # --- 3. Screening 실행 (Track D stub) ---
    # JSON 직렬화 가능해야 subprocess/docker backend이 stdin으로 넘길 수 있다.
    # (Stub은 무시하므로 어느 모드든 무해)
    market_data_records: list[dict] = []
    if comp.db_engine is not None and scout_ctx.universe:
        from .scout.market_data_loader import load_market_data_records

        market_data_records = await load_market_data_records(
            comp.db_engine,
            universe=scout_ctx.universe,
            as_of=as_of_date,
            lookback_days=int(os.environ.get("SCOUT_MARKET_DATA_LOOKBACK_DAYS", "60")),
        )

    screening_context = {
        "as_of": as_of_date.isoformat(),
        "universe": scout_ctx.universe,
        "news_scores": {t: e.model_dump(mode="json") for t, e in scout_ctx.news_scores.items()},
        "sector_momentum": scout_ctx.sector_momentum,
        "macro_size_multiplier": post.output.size_multiplier,
        "market_data_records": market_data_records,
    }
    raw_candidates: list[ScreeningCandidate] = await comp.screening.invoke(
        scout_out.screening_code, screening_context
    )

    # raw 후보 전수를 screening_candidates 에 기록 (백테스트 재현용).
    # 이후 validation / engine 결정에 따라 promoted_to_sheet_id 또는
    # rejection_reason 이 채워진다.
    await persist_screening_candidates(
        comp.db_engine,
        scout_run_id=scout_run_id,
        candidates=raw_candidates,
    )

    # --- 4. 검증 ---
    validation = validate_candidates(raw_candidates, scout_ctx.universe)

    # validation 에서 탈락 (universe 밖 = hallucination) 한 ticker 기록
    if validation.hallucinated_tickers:
        for _t in validation.hallucinated_tickers:
            await update_candidate_promotion(
                comp.db_engine,
                scout_run_id=scout_run_id,
                ticker=_t,
                rejection_reason="validator_hallucination",
            )

    if validation.hallucination_fail:
        await observer.emit(
            pj_event(
                "pj.scout.hallucination_suspected",
                role="scout",
                ok=False,
                severity="fail",
                hallucinated=validation.hallucinated_tickers,
            )
        )
        return SlowLoopResult(
            macro_post=post,
            scout_output=scout_out,
            validation=validation,
            skipped_reason="scout_hallucination",
        )
    if validation.hallucination_warn:
        await observer.emit(
            pj_event(
                "pj.scout.hallucination_suspected",
                role="scout",
                ok=False,
                severity="warn",
                hallucinated=validation.hallucinated_tickers,
            )
        )

    if not validation.candidates:
        await observer.emit(
            pj_event(
                "pj.scout.no_candidates",
                role="scout",
                ok=True,
                fallback=scout_out.fallback_strategy,
            )
        )
        return SlowLoopResult(
            macro_post=post,
            scout_output=scout_out,
            validation=validation,
            skipped_reason="no_candidates",
        )

    # --- 5. Strategy + 발행 ---
    inputs = StrategyEngineInputs(
        macro_state=MacroStateSnapshot(
            gate=post.output.gate,
            size_multiplier=post.output.size_multiplier,
            gate_run_id=macro_run_id,
        ),
        scout_run_id=scout_run_id,
        scout_code_hash=compute_code_hash(scout_out.screening_code),
        scout_hypothesis=scout_out.hypothesis,
        generated_at=as_of_dt,
        news_score=None,  # Phase 2: scout_ctx.news_scores에서 per-ticker로
    )

    published: list[str] = []
    rejected: list[str] = []
    for cand in validation.candidates:
        try:
            sheet, reject_reason = await comp.engine.build_sheet_with_reason(cand, inputs)
        except Exception:
            logger.exception("engine.build_sheet raised for %s", cand.ticker)
            rejected.append(cand.ticker)
            await update_candidate_promotion(
                comp.db_engine,
                scout_run_id=scout_run_id,
                ticker=cand.ticker,
                rejection_reason="engine_error",
            )
            await observer.emit(
                pj_event(
                    "pj.strategy.sheet_error",
                    role="strategy",
                    ok=False,
                    ticker=cand.ticker,
                )
            )
            continue

        if sheet is None:
            rejected.append(cand.ticker)
            await update_candidate_promotion(
                comp.db_engine,
                scout_run_id=scout_run_id,
                ticker=cand.ticker,
                rejection_reason=reject_reason or "engine_rejected",
            )
            await observer.emit(
                pj_event(
                    "pj.strategy.sheet_rejected",
                    role="strategy",
                    ok=True,
                    ticker=cand.ticker,
                )
            )
            continue

        try:
            await comp.publisher.publish(sheet)
        except Exception:
            logger.exception("publisher.publish failed for %s", sheet.sheet_id)
            rejected.append(cand.ticker)
            await update_candidate_promotion(
                comp.db_engine,
                scout_run_id=scout_run_id,
                ticker=cand.ticker,
                rejection_reason="publisher_error",
            )
            continue

        published.append(sheet.sheet_id)
        await update_candidate_promotion(
            comp.db_engine,
            scout_run_id=scout_run_id,
            ticker=cand.ticker,
            sheet_id=sheet.sheet_id,
        )
        await observer.emit(
            pj_event(
                "pj.strategy.sheet_published",
                role="strategy",
                ok=True,
                ticker=cand.ticker,
                sheet_id=sheet.sheet_id,
                final_pct=sheet.size.final_pct,
            )
        )

    return SlowLoopResult(
        macro_post=post,
        scout_output=scout_out,
        validation=validation,
        sheets_published=published,
        sheets_rejected=rejected,
    )


__all__ = [
    "SlowLoopComponents",
    "SlowLoopResult",
    "run_slow_loop",
]
