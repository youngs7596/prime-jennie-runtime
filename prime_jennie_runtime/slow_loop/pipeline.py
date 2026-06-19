"""Slow Loop Pipeline 조립.

Phase 0 design §4 (2026-05-22 결정론 코어 전환 반영). 구조:
  1. Macro Gate role (LLM fast path) + 결정론 후처리 (auto_override, discretize, 이벤트)
  2. macro:current_state 저장 + stale 체크
  3. 결정론 Scout (deterministic_scout.run_deterministic_scout — LLM 호출 0회)
  4. 후보 검증 (validate_candidates — universe / 중복 / cap / hallucination)
  5. Strategy Engine.build_sheet × N
  6. PositionSheetPublisher.publish

LLM 호출은 Macro gate 한 번뿐 (+선택적 shadow). Scout 는 결정론 quant 코어로
포팅됐다 (2026-05-22). minyoung-mah에 없는 조건부 분기/retry를 local workaround로.

DLQ 정책 (2026-05-11):
- `publisher.publish` 의 stream 발행 실패는 publisher 내부에서 raw sheet JSON 을
  DLQ 로 송부 후 재던짐 — pipeline 은 `publisher_error` 거부 사유만 남긴다.
- pipeline 레벨에서 추가로 DLQ 송부하는 경우:
    1. Scout hallucination_fail — universe 밖 ticker 비율 ≥ 50% 이면 raw candidate
       payload 전수를 DLQ 로 (이후 모델 회귀 분석용).
    2. engine.build_sheet 예외 — sheet 가 만들어지지 못한 경우 raw candidate
       payload + error 메시지를 DLQ 로 (시트 schema 변경 등에서 잡힘).
  publisher 의 publish 실패는 이미 raw sheet JSON 이 DLQ 로 가므로 중복 송부하지
  않는다 (위 두 경로와 사유 분리: dlq_reason 메타데이터를 raw payload 안에 포함).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from minyoung_mah import (
    InvocationContext,
    Observer,
    Orchestrator,
    PipelineStep,
    StaticPipeline,
)

from prime_jennie_runtime.control.state import SystemState
from prime_jennie_runtime.infra.observer_impl import pj_event
from prime_jennie_runtime.position_sheet.schema import MacroStateSnapshot

from .macro.context_builder import MacroContextBuilder
from .macro.post_processor import MacroPostResult, run_post_processing
from .macro.schemas import MacroGateOutput, RecentMacroRun
from .macro.state_store import MacroCurrentState, MacroStateStore
from .persistence import (
    fetch_previous_run_candidates,
    persist_macro_run,
    persist_scout_run,
    persist_screening_candidates,
    update_candidate_promotion,
)
from .scout.candidate_validation import ScoutValidationResult, validate_candidates
from .scout.context_builder import ScoutContextBuilder
from .scout.schemas import (
    MacroStateForScout,
    ScoutOutput,
    ScoutRunSummary,
    ScreeningCandidate,
)
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


async def _dlq_send(
    publisher: PositionSheetPublisher,
    *,
    reason: str,
    payload: Any,
    error: str,
    observer: Observer | None = None,
) -> None:
    """Pipeline 단계 실패 시 DLQ 송부 헬퍼.

    publisher 내부 DLQ 채널을 그대로 재사용. `dlq_reason` 메타데이터를 payload
    JSON 에 포함시켜 publisher 자체 stream 실패와 구분 가능. DLQ 송부 자체가
    실패하면 swallow (로깅) — pipeline 의 후속 정상 처리를 막지 않는다.
    """
    try:
        raw = {
            "dlq_reason": reason,
            "payload": payload,
        }
        await publisher.send_raw_to_dlq(
            raw_payload=json.dumps(raw, ensure_ascii=False, default=str),
            error=error,
        )
        if observer is not None:
            await observer.emit(
                pj_event(
                    "pj.slow_loop.dlq_sent",
                    role="slow_loop",
                    ok=True,
                    reason=reason,
                )
            )
    except Exception:
        logger.exception("dlq_send failed reason=%s", reason)


async def _run_macro_with_retry(
    orchestrator: Orchestrator,
    macro_ctx: Any,
    observer: Observer,
    *,
    role_name: str = "macro_gate",
    max_attempts: int = 3,
    user_request: str = "daily macro gate",
) -> Any:
    """Macro 전용 재시도 — 매 시도마다 ``previous_attempts`` 를 ctx 에 주입.

    직전 시도의 실패 사유를 LLM 에 그대로 보여줘 같은 실수 반복을 차단한다.
    context 가 매 시도 변하므로 ``_macro_pipeline(ctx)`` 를 매번 새로 조립.
    """
    from .macro.schemas import MacroAttemptHint

    attempts: list[MacroAttemptHint] = []
    last_error: str | None = None

    for attempt_no in range(1, max_attempts + 1):
        attempt_ctx = macro_ctx.model_copy(update={"previous_attempts": list(attempts)})
        try:
            result = await orchestrator.run_pipeline(
                _macro_pipeline(attempt_ctx), user_request=user_request
            )
            if result.completed:
                return result
            last_error = result.error or f"aborted at step {result.aborted_at}"
        except Exception as e:  # noqa: BLE001
            last_error = f"{type(e).__name__}: {e}"

        # 다음 시도에 주입할 hint 누적
        attempts.append(
            MacroAttemptHint(
                attempt_no=attempt_no,
                error="role_invocation_failed",
                details=(last_error or "(no details)")[:2000],
            )
        )
        await observer.emit(
            pj_event(
                f"pj.{role_name}.retry",
                role=role_name,
                ok=False,
                attempt=attempt_no,
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
    engine: StrategyEngine
    publisher: PositionSheetPublisher
    state_store: MacroStateStore
    observer: Observer
    db_engine: Any = None  # AsyncEngine | None — macro_runs/scout_runs 기록용
    shadow_orchestrator: Any = None  # Orchestrator | None — Macro 를 DeepSeek 로 shadow 평가
    redis_client: Any = None  # aioredis.Redis | None — LLM stats 누적용 (llm:stats:{date}:{svc})
    system_state: SystemState | None = None  # control.state:* 읽기. None 이면 STOP/PAUSE 미체크.
    # scout 단계 주입점 — None 이면 deterministic_scout.run_deterministic_scout 사용.
    # 테스트가 canned 후보를 주입할 때 fake runner 를 넣는다 (engine/checker 와 동일 패턴).
    scout_runner: Any = None


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

    # --- 0. control.state 게이트는 시트 발행 직전(섹션 5)으로 이동 ---
    # STOP/PAUSE 여도 Macro/Scout 분석 + DB 영속(macro_runs/scout_runs/
    # screening_candidates/position_sheets)은 정상 수행하고, fast_loop 핸드오프
    # (Redis stream emit)만 차단한다. 이유: STOP 구간에도 관측·백테스트 데이터가
    # 남아야 한다 (게이트를 최상단에 두면 STOP 동안 분석 데이터가 전부 0 이 됨).
    # 진입 차단의 최종 안전망은 fast_loop entry path 가 control.state 를 독립
    # 확인하는 것 (defense-in-depth).

    # --- 0.5. Risk Throttle 스냅샷 갱신 ---
    # fast_loop risk_updater 가 Redis 에 적재한 최신 level 을 engine 이 시트 발행 시
    # 사용할 수 있도록 한 번 갱신. 실패는 fail-open (기존 캐시 또는 1.0 유지).
    risk_snap = getattr(comp.engine, "_risk", None)
    if risk_snap is not None and hasattr(risk_snap, "refresh"):
        try:
            await risk_snap.refresh()
        except Exception:
            logger.exception("risk throttle refresh failed — using last known multiplier")

    # --- 0.7. control.state 스냅샷 (run 전체에서 한 번만 읽음) ---
    # paper 모드 (STOP) 여부를 두 곳에서 쓴다: (a) macro 후처리의 reversal guard paper
    # 완화 (P2.7), (b) 섹션 5 의 fast_loop stream emit 차단. 한 번만 읽어 run 안에서
    # 같은 상태를 보게 한다 — 중간에 /resume 이 끼어들어도 반쪽 적용이 안 되도록.
    sys_snap = None
    if comp.system_state is not None:
        try:
            sys_snap = await comp.system_state.snapshot()
        except Exception:
            logger.exception("control.state snapshot failed — 비-paper (latch 유지) 로 진행")

    # --- 1. Macro phase ---
    macro_ctx = await comp.macro_builder.build(
        as_of=as_of_dt,
        recent_runs=recent_macro_runs,
        trigger_reason=macro_trigger,
    )

    async def _primary_macro():
        return await _run_macro_with_retry(
            comp.orchestrator,
            macro_ctx,
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
            res = await _run_macro_with_retry(
                comp.shadow_orchestrator,
                macro_ctx,
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
        engine=comp.db_engine,
        macro_run_id=macro_run_id,
        as_of=as_of_dt,
        # paper 모드 (STOP) 에서는 reversal latch 를 완화 — 메타만 기록 (P2.7).
        paper_mode=bool(sys_snap is not None and sys_snap.stopped),
    )

    # DB 기록 (engine=None 이면 no-op). prompt_chars 는 비용 추정용.
    from .macro.prompts import MACRO_PROMPT_VERSION, MACRO_SYSTEM_PROMPT
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
        prompt_version=MACRO_PROMPT_VERSION,
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

    # Macro gate closed.
    # 종전에는 비용 절감 차 Scout phase 를 통째로 생략하고 early-return 했다.
    # 그러나 paper alpha 실험에서 이는 데이터 파괴다 — 닫힌 날의 점수·후보가
    # 영영 안 남아 "게이트가 옳았나"를 사후 측정할 길이 사라진다. STOP/PAUSE 도
    # 같은 이유로 "분석·영속은 수행하되 fast_loop 핸드오프만 차단" 으로 옮겨갔다
    # (섹션 0 주석). macro_closed 도 동일 원칙을 따른다: Scout 를 그대로 돌려
    # daily_quant_scores(sector_momentum 포함)·screening_candidates 를 남기고,
    # 매매 핸드오프 직전에 멈춘다. 결정론 Scout 는 LLM 호출 0회라 비용도 미미하다.
    gate_closed = post.output.gate == "closed"
    if gate_closed:
        await observer.emit(
            pj_event(
                "pj.slow_loop.skipped_macro_closed",
                role="slow_loop",
                ok=True,
                triggers=list(post.closed_triggers),
            )
        )

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

    # --- 2b. Scout phase — 결정론 quant 스코어러 (2026-05-22 Phase 1 a) ---
    # 매 실행 LLM codegen(code_loop) 은퇴. v2 결정론 선정 코어(quant.py 7팩터 +
    # MA 평활 + 히스테리시스)의 포팅본을 호출한다 — 선정 경로 LLM 호출 0회.
    # scout shadow(Opus codegen) 도 함께 은퇴 — codegen 이 없으면 비교 대상 무의미.
    # 결정 기록: .ai/decisions/2026-05-22-selection-architecture-decision.md
    import hashlib as _hashlib

    from .scout.deterministic_scout import (
        SCORER_VERSION,
        compute_code_hash,
        run_deterministic_scout,
    )

    _scout_runner = comp.scout_runner or run_deterministic_scout
    validated_scout = await _scout_runner(
        db_engine=comp.db_engine,
        scout_ctx=scout_ctx,
        as_of=as_of_date,
        as_of_dt=as_of_dt,
        scout_run_id=scout_run_id,
    )
    scout_out: ScoutOutput = validated_scout.scout_out
    raw_candidates: list[ScreeningCandidate] = list(validated_scout.result.candidates)

    await observer.emit(
        pj_event(
            "pj.scout.code_generated",
            role="scout",
            ok=True,
            hypothesis=scout_out.hypothesis,
            expected_candidates=scout_out.expected_candidates,
        )
    )

    # context snapshot — 백테스트 재현용 (scout_runs.context_snapshot_json).
    # universe 는 해시 + size 만 (full list 는 universe_hash 일치하는 외부 테이블 복원).
    _universe_raw = ",".join(sorted(scout_ctx.universe))
    scout_context_snapshot = {
        "as_of": as_of_date.isoformat(),
        "trigger_reason": scout_trigger,
        "universe_size": len(scout_ctx.universe),
        "universe_hash": _hashlib.sha256(_universe_raw.encode("utf-8")).hexdigest(),
        "sector_momentum": dict(scout_ctx.sector_momentum),
        "macro_size_multiplier": float(post.output.size_multiplier),
        "macro_gate": post.output.gate,
        "macro_run_id": macro_run_id,
        "scorer_version": SCORER_VERSION,
    }

    # --- 3. DB 기록 ---
    # 결정론 스코어러는 LLM 호출 0회 — model_used/cost_usd 는 NULL, LLM 사용량
    # 통계도 미적재. prompt_version 컬럼에는 스코어러 버전(SCORER_VERSION)을 기록.
    await persist_scout_run(
        comp.db_engine,
        scout_run_id=scout_run_id,
        generated_at=as_of_dt,
        scout_out=scout_out,
        candidates_count=len(raw_candidates),
        scorer_version=SCORER_VERSION,
        context_snapshot=scout_context_snapshot,
    )

    # raw 후보 전수를 screening_candidates 에 기록. persist_scout_run 후에 호출
    # — screening_candidates.scout_run_id → scout_runs FK 제약 (순서 중요).
    await persist_screening_candidates(
        comp.db_engine,
        scout_run_id=scout_run_id,
        candidates=raw_candidates,
    )

    # paper alpha shadow — macro 게이트가 닫힌 날도 점수·후보까지는 영속했다.
    # gate=closed 면 size_multiplier=0 이라 engine 이 시트를 전부 None 처리하고,
    # 닫힌 날 매매도 없어야 하므로 시트 build/발행 단계로 진행하지 않는다. 여기서
    # 멈춰도 반사실 측정에 필요한 데이터(점수·후보)는 확보된다 — 후보의 forward
    # return 은 daily_prices 로 별도 산출(factor IC 분석과 동일 방식).
    if gate_closed:
        return SlowLoopResult(
            macro_post=post,
            scout_output=scout_out,
            skipped_reason="macro_closed_shadow",
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
        # DLQ — universe 밖 ticker 비율 ≥ 50% 이면 raw candidates 전수를
        # DLQ 로 송부해 모델 회귀 분석용으로 보존 (시트는 발행되지 못함).
        await _dlq_send(
            comp.publisher,
            reason="scout_hallucination_fail",
            payload={
                "scout_run_id": scout_run_id,
                "hallucinated_tickers": list(validation.hallucinated_tickers),
                "raw_candidates": [c.model_dump(mode="json") for c in raw_candidates],
            },
            error=(
                f"hallucinated ratio >= 50%: "
                f"{len(validation.hallucinated_tickers)}/{len(raw_candidates)}"
            ),
            observer=observer,
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

        # use_previous_run fallback — Scout 가 명시한 경우만 활성.
        # 24h 내 직전 success run 의 candidates 를 동일 macro_state 일 때 재사용.
        if scout_out.fallback_strategy == "use_previous_run":
            prev = await fetch_previous_run_candidates(
                comp.db_engine,
                as_of=as_of_dt,
                macro_gate=post.output.gate,
                macro_size_multiplier=float(post.output.size_multiplier),
            )
            if prev is not None:
                prev_run_id, prev_candidates = prev
                # universe 필터 한 번 더 — 24h 전 universe 와 다를 수 있음.
                reused_validation = validate_candidates(prev_candidates, scout_ctx.universe)
                if reused_validation.candidates:
                    await observer.emit(
                        pj_event(
                            "pj.scout.fallback_use_previous_run",
                            role="scout",
                            ok=True,
                            previous_scout_run_id=prev_run_id,
                            reused_count=len(reused_validation.candidates),
                        )
                    )
                    validation = reused_validation  # 이후 sheet 발행 흐름이 그대로 동작
                else:
                    await observer.emit(
                        pj_event(
                            "pj.scout.fallback_use_previous_run_empty",
                            role="scout",
                            ok=False,
                            previous_scout_run_id=prev_run_id,
                            reason="all_outside_universe",
                        )
                    )
            else:
                await observer.emit(
                    pj_event(
                        "pj.scout.fallback_use_previous_run_unavailable",
                        role="scout",
                        ok=False,
                        reason="no_matching_previous_run",
                    )
                )

        if not validation.candidates:
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
        news_score=None,  # Phase 2: scout_ctx.news_events에서 per-ticker로
    )

    # control.state 게이트 (섹션 0 에서 이리로 이동) — STOP/PAUSE 면 시트를
    # build + position_sheets DB 영속까지는 수행하되 fast_loop 핸드오프
    # (Redis stream emit)만 차단한다. Macro/Scout 분석·영속은 위에서 이미 끝나
    # macro_runs/scout_runs/screening_candidates 가 STOP 중에도 남는다.
    # fast_loop entry path 가 control.state 를 독립 확인하므로 만에 하나 stream
    # 으로 샜더라도 진입은 0 (defense-in-depth).
    # 스냅샷은 섹션 0.7 에서 읽은 것을 재사용 — run 안에서 상태 일관성 유지.
    emit_to_fast_loop = True
    control_reason: str | None = None
    if sys_snap is not None:
        if sys_snap.stopped:
            emit_to_fast_loop, control_reason = False, "control_stopped"
        elif sys_snap.paused:
            emit_to_fast_loop, control_reason = False, "control_paused"
    if not emit_to_fast_loop:
        logger.warning(
            "slow_loop: control %s — 시트 build + DB 영속은 유지, fast_loop stream emit 만 skip",
            control_reason,
        )
        await observer.emit(
            pj_event(
                "pj.slow_loop.publish_blocked_control",
                role="slow_loop",
                ok=True,
                reason=control_reason,
            )
        )

    published: list[str] = []
    rejected: list[str] = []
    persisted_sheets = []  # 추천 발송용 — emit 여부 무관, DB 영속 성공한 시트
    for cand in validation.candidates:
        try:
            sheet, reject_reason = await comp.engine.build_sheet_with_reason(cand, inputs)
        except Exception as exc:  # noqa: BLE001 — engine 거부는 모든 예외 흡수 후 DLQ.
            logger.exception("engine.build_sheet raised for %s", cand.ticker)
            rejected.append(cand.ticker)
            await update_candidate_promotion(
                comp.db_engine,
                scout_run_id=scout_run_id,
                ticker=cand.ticker,
                rejection_reason="engine_error",
            )
            # DLQ — sheet 가 만들어지지 못했으니 publisher 가 못 잡는다.
            # raw candidate payload + error 컨텍스트 송부.
            await _dlq_send(
                comp.publisher,
                reason="engine_build_sheet_error",
                payload={
                    "scout_run_id": scout_run_id,
                    "candidate": cand.model_dump(mode="json"),
                },
                error=f"{type(exc).__name__}: {exc}",
                observer=observer,
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
            await comp.publisher.publish(sheet, emit_stream=emit_to_fast_loop)
        except Exception as pub_exc:  # noqa: BLE001
            logger.exception("publisher.publish failed for %s", sheet.sheet_id)
            rejected.append(cand.ticker)
            await update_candidate_promotion(
                comp.db_engine,
                scout_run_id=scout_run_id,
                ticker=cand.ticker,
                rejection_reason="publisher_error",
            )
            # publisher 가 stream 발행 단계에서 실패했을 때만 raw sheet 를 DLQ 로
            # 보낸다 (publisher._send_to_dlq 내부 호출). 그러나 _persist_sheet (DB
            # upsert) 단계의 실패는 DLQ 미송부 — pipeline 에서 보강.
            await _dlq_send(
                comp.publisher,
                reason="publisher_error",
                payload={
                    "scout_run_id": scout_run_id,
                    "sheet": sheet.model_dump(mode="json"),
                },
                error=f"{type(pub_exc).__name__}: {pub_exc}",
                observer=observer,
            )
            continue

        # 시트는 position_sheets 에 영속됨 — emit 여부와 무관하게 promotion FK 기록.
        await update_candidate_promotion(
            comp.db_engine,
            scout_run_id=scout_run_id,
            ticker=cand.ticker,
            sheet_id=sheet.sheet_id,
        )
        persisted_sheets.append(sheet)

        if not emit_to_fast_loop:
            # control STOP/PAUSE — 시트는 DB 에 영속됐으나 fast_loop 로 stream
            # emit 하지 않는다. 관측/백테스트 데이터로만 남는다 (진입 0).
            await observer.emit(
                pj_event(
                    "pj.strategy.sheet_persisted_no_emit",
                    role="strategy",
                    ok=True,
                    ticker=cand.ticker,
                    sheet_id=sheet.sheet_id,
                    reason=control_reason,
                )
            )
            continue

        published.append(sheet.sheet_id)
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

        # Coordinator Event Bus hook (Stage 1, best-effort).
        # publish 가 이미 성공한 후 발행 — 실패해도 매매 path 영향 X.
        try:
            from prime_jennie_runtime.coordinator import (
                SheetPublishedEvent,
                publish_event,
            )

            await publish_event(
                comp.publisher.client,
                SheetPublishedEvent(
                    occurred_at=datetime.now(UTC),
                    correlation_id=sheet.sheet_id,
                    sheet_id=sheet.sheet_id,
                    ticker=sheet.ticker,
                    strategy_tag=sheet.strategy_tag,
                    conviction=sheet.provenance.conviction,
                    final_pct=sheet.size.final_pct,
                    macro_run_id=macro_run_id,
                    scout_run_id=scout_run_id,
                    macro_gate=post.output.gate,
                    macro_size_mult=float(post.output.size_multiplier),
                    risk_level=None,
                    sheet_generated_at=sheet.generated_at,
                ),
            )
        except Exception:
            logger.exception(
                "coordinator publish_event(SheetPublished) failed sheet=%s", sheet.sheet_id
            )

    # --- 6. 추천 발송 (시나리오 B) — 시트 영속까지 끝난 뒤 best-effort ---
    # STOP 중에는 발송하지 않는다 (수락해도 consumer 가 차단하므로 소음만 됨).
    # PAUSE (사람-승인 운영) 가 주 대상이고, 자동매매 상태에서도 참고용 발송.
    if persisted_sheets and control_reason != "control_stopped":
        try:
            from prime_jennie_runtime.slow_loop.recommendation import (
                announce_recommendations,
            )

            await announce_recommendations(
                comp.publisher.client,
                comp.db_engine,
                persisted_sheets,
                as_of_dt=as_of_dt,
            )
        except Exception:
            logger.exception("recommendation announce failed (best-effort)")

    return SlowLoopResult(
        macro_post=post,
        scout_output=scout_out,
        validation=validation,
        sheets_published=published,
        sheets_rejected=rejected,
        # control STOP/PAUSE 면 사유를 남긴다 — 시트는 영속됐으나 stream emit 은
        # skip 됐음을 호출부(slow_loop/app.py 로그)가 인지하도록.
        skipped_reason=control_reason,
    )


__all__ = [
    "SlowLoopComponents",
    "SlowLoopResult",
    "run_slow_loop",
]
