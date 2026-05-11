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

    단일 role의 구조화 출력 파싱이 드물게 실패할 때를 위한 방어망. 시도마다
    동일 pipeline 객체를 그대로 재호출하므로 context 갱신은 지원하지 않는다 —
    그런 경우 ``_run_macro_with_retry`` 처럼 매 시도마다 pipeline 을 재조립한다.
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

    Scout 의 ``generate_validated_code`` 와 동일 사상: 직전 시도의 실패 사유를
    LLM 에 그대로 보여줘 같은 실수 반복을 차단한다. context 가 매 시도 변하므로
    ``_macro_pipeline(ctx)`` 를 매번 새로 조립.
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
    screening: ScreeningInvoker
    engine: StrategyEngine
    publisher: PositionSheetPublisher
    state_store: MacroStateStore
    observer: Observer
    db_engine: Any = None  # AsyncEngine | None — macro_runs/scout_runs 기록용
    shadow_orchestrator: Any = None  # Orchestrator | None — Macro 를 DeepSeek 로 shadow 평가
    redis_client: Any = None  # aioredis.Redis | None — LLM stats 누적용 (llm:stats:{date}:{svc})
    system_state: SystemState | None = None  # control.state:* 읽기. None 이면 STOP/PAUSE 미체크.


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

    # --- 0. control.state 게이트 ---
    # /pause /stop 같은 긴급 제어가 활성이면 Macro/Scout LLM 호출 + 시트 발행 모두
    # skip. fast_loop entry_executor 가 이미 진입을 차단하지만, 새 시트 발행 자체를
    # 막지 않으면 사용자가 STOP 후에도 자꾸 sheet 가 쌓여 confusing. 따라서 사용자
    # 의도(긴급 제어)를 우선해 가장 앞단에서 막는다.
    if comp.system_state is not None:
        sys_snap = await comp.system_state.snapshot()
        if sys_snap.stopped:
            logger.warning(
                "slow_loop skipped: SystemState.stopped — sheet 발행 + LLM 호출 모두 차단"
            )
            await observer.emit(
                pj_event(
                    "pj.slow_loop.skipped_control",
                    role="slow_loop",
                    ok=True,
                    reason="stopped",
                )
            )
            return SlowLoopResult(skipped_reason="control_stopped")
        if sys_snap.paused:
            logger.warning(
                "slow_loop skipped: SystemState.paused (reason=%s) — sheet 발행 + LLM 호출 차단",
                sys_snap.pause_reason,
            )
            await observer.emit(
                pj_event(
                    "pj.slow_loop.skipped_control",
                    role="slow_loop",
                    ok=True,
                    reason="paused",
                    pause_reason=sys_snap.pause_reason,
                )
            )
            return SlowLoopResult(skipped_reason="control_paused")

    # --- 0.5. Risk Throttle 스냅샷 갱신 ---
    # fast_loop risk_updater 가 Redis 에 적재한 최신 level 을 engine 이 시트 발행 시
    # 사용할 수 있도록 한 번 갱신. 실패는 fail-open (기존 캐시 또는 1.0 유지).
    risk_snap = getattr(comp.engine, "_risk", None)
    if risk_snap is not None and hasattr(risk_snap, "refresh"):
        try:
            await risk_snap.refresh()
        except Exception:
            logger.exception("risk throttle refresh failed — using last known multiplier")

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

    # market_data 와 screening_context 를 검증 루프 호출 전에 미리 준비
    # (검증 루프 안에서 sandbox 실행 시 필요)
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
        "news_events": {t: e.model_dump(mode="json") for t, e in scout_ctx.news_events.items()},
        "sector_momentum": scout_ctx.sector_momentum,
        "macro_size_multiplier": post.output.size_multiplier,
        "market_data_records": market_data_records,
        # Forward 컨센서스 — 현재 EmptyConsensusFeeder 라 None 값만 들어옴.
        # Scout 코드는 missing 으로 취급. 후속 작업에서 RealConsensusFeeder 가 채움.
        "consensus_data": {
            t: e.model_dump(mode="json") for t, e in scout_ctx.consensus_data.items()
        },
    }

    async def _primary_scout_validated():
        """Scout LLM ↔ sandbox 검증 닫힌 루프 (최대 3회)."""
        from .scout.code_loop import generate_validated_code

        return await generate_validated_code(
            orch=comp.orchestrator,
            scout_ctx=scout_ctx,
            screening=comp.screening,
            screening_context=screening_context,
            observer=observer,
            scout_run_id=scout_run_id,
        )

    async def _shadow_scout():
        """Scout shadow — DeepSeek chat 으로 같은 pipeline 을 병렬 평가.

        Macro shadow 와 동일 패턴. 실패해도 primary 결정을 방해하지 않고 None 반환.
        duration_ms + metadata (usage) 를 payload 에 실어 cost 추정에 활용.
        """
        if comp.shadow_orchestrator is None:
            return None
        import time as _t

        t0 = _t.monotonic()
        try:
            res = await _run_pipeline_with_retry(
                comp.shadow_orchestrator,
                _scout_pipeline(scout_ctx),
                observer,
                role_name="scout_shadow",
                user_request="daily scout run (shadow)",
            )
            shadow_meta: dict[str, Any] = {}
            try:
                step = res.state["scout"]
                if step.outputs:
                    shadow_meta = dict(step.outputs[0].metadata or {})
            except Exception:
                shadow_meta = {}
            return {
                "result": res,
                "duration_ms": int((_t.monotonic() - t0) * 1000),
                "metadata": shadow_meta,
            }
        except Exception as e:
            logger.warning("scout shadow failed: %s", e)
            return {"error": f"{type(e).__name__}: {e}"}

    validated_scout, scout_shadow_payload = await asyncio.gather(
        _primary_scout_validated(), _shadow_scout(), return_exceptions=False
    )
    scout_out: ScoutOutput = validated_scout.scout_out
    scout_step_result = validated_scout.scout_step_result
    if scout_step_result is None:
        # scout LLM 호출 자체가 한 번도 성공 못함
        await observer.emit(pj_event("pj.scout.output_missing", role="scout", ok=False))
        return SlowLoopResult(macro_post=post, skipped_reason="scout_output_missing")

    # DB 기록 (engine=None 이면 no-op). candidates_count 는 screening 코드를
    # sandbox 실행해 얻은 실제 후보 수 (raw_candidates) 로 채운다 — LLM 예측치
    # (scout_out.expected_candidates) 는 모니터링 정확도를 떨어뜨려 사용 금지.
    from .scout.prompts import SCOUT_PROMPT_VERSION, SCOUT_SYSTEM_PROMPT
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
        "news_events": {t: e.model_dump(mode="json") for t, e in scout_ctx.news_events.items()},
        "sector_momentum": dict(scout_ctx.sector_momentum),
        "macro_size_multiplier": float(post.output.size_multiplier),
        "macro_gate": post.output.gate,
        "macro_run_id": macro_run_id,
    }

    # Shadow ScoutOutput 추출 (screening 실행에 shadow code 가 필요)
    shadow_scout_out: ScoutOutput | None = None
    if scout_shadow_payload is not None and "error" not in scout_shadow_payload:
        try:
            shadow_res = scout_shadow_payload["result"]
            shadow_scout_out = shadow_res.state["scout"].payload_as(ScoutOutput)
        except Exception:
            logger.exception("scout shadow result extract failed")

    # persist_scout_run 은 screening 결과까지 포함한 shadow_result 를 원하므로
    # screening 실행 블록 이후에 호출. 아래 섹션 3 뒤로 이동.

    await observer.emit(
        pj_event(
            "pj.scout.code_generated",
            role="scout",
            ok=True,
            hypothesis=scout_out.hypothesis,
            expected_candidates=scout_out.expected_candidates,
        )
    )

    # --- 3. Screening 결과 (검증 루프에서 이미 sandbox 실행 완료) ---
    # primary candidates 는 검증 루프가 누적한 마지막 시도의 결과 그대로.
    # shadow 는 별도 1회 실행 (shadow 는 검증 없이 비교 전용).
    raw_candidates: list[ScreeningCandidate] = list(validated_scout.result.candidates)

    async def _shadow_screen() -> list[ScreeningCandidate]:
        """Shadow scout 가 생성한 Python 코드를 격리 실행해 후보 추출.

        primary 와 **동일 screening_context** 로 병렬 실행하므로 공정 비교 가능.
        shadow 가 없거나 코드가 비어있으면 빈 리스트. 실행 실패는 swallow 후 빈 리스트.
        shadow 후보는 screening_candidates 테이블에 기록하지 않고 metadata.shadow.candidates
        JSON 으로만 보존 — FK 복잡도 회피 + primary 와 비교 전용이라는 의미 명확.
        """
        if shadow_scout_out is None or not shadow_scout_out.screening_code:
            return []
        try:
            return await comp.screening.invoke(shadow_scout_out.screening_code, screening_context)
        except Exception:
            logger.exception("shadow screening failed")
            return []

    shadow_raw_candidates = await _shadow_screen()

    # Scout shadow 결과 구성 — DeepSeek 의 hypothesis + code + candidates 까지 포함
    scout_shadow_for_db: dict[str, Any] | None = None
    if shadow_scout_out is not None:
        try:
            from .persistence import _resolve_cost, _tier_model

            shadow_model_name = _tier_model("shadow_strong")  # DeepSeek chat
            shadow_meta = (
                (scout_shadow_payload.get("metadata") or {})
                if scout_shadow_payload is not None
                else {}
            )
            shadow_out_chars = len(shadow_scout_out.screening_code or "") + len(
                shadow_scout_out.hypothesis or ""
            )
            shadow_cost_est = _resolve_cost(
                shadow_meta, shadow_model_name, scout_prompt_chars, shadow_out_chars
            )
            scout_shadow_for_db = {
                "model_used": shadow_model_name,
                "hypothesis": shadow_scout_out.hypothesis,
                "code_hash": _hashlib.sha256(
                    (shadow_scout_out.screening_code or "").encode("utf-8")
                ).hexdigest(),
                "code_text": shadow_scout_out.screening_code,
                "expected_candidates": shadow_scout_out.expected_candidates,
                "strategy_tags_used": list(shadow_scout_out.strategy_tags_used or []),
                "latency_ms": (
                    scout_shadow_payload.get("duration_ms")
                    if scout_shadow_payload is not None
                    else None
                ),
                "cost_usd_estimated": shadow_cost_est,
                # Shadow candidates — primary 와 동일 context 로 실행된 raw 후보 전수.
                # 20개 HARD CAP 적용됨 (executor.py). DB 에 JSON 으로 저장.
                "candidates": [c.model_dump(mode="json") for c in shadow_raw_candidates],
            }
        except Exception:
            logger.exception("scout shadow payload build failed")
    elif scout_shadow_payload and "error" in scout_shadow_payload:
        scout_shadow_for_db = {"error": scout_shadow_payload["error"]}

    await persist_scout_run(
        comp.db_engine,
        scout_run_id=scout_run_id,
        generated_at=as_of_dt,
        scout_out=scout_out,
        scout_step_result=scout_step_result,
        candidates_count=len(raw_candidates),
        prompt_chars=scout_prompt_chars,
        prompt_version=SCOUT_PROMPT_VERSION,
        context_snapshot=scout_context_snapshot,
        shadow_result=scout_shadow_for_db,
        redis_client=comp.redis_client,
    )

    # raw 후보 전수를 screening_candidates 에 기록 (백테스트 재현용).
    # persist_scout_run 후에 호출 — screening_candidates.scout_run_id 가 scout_runs
    # 를 참조하는 FK 제약이 있어 순서가 중요함.
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

    published: list[str] = []
    rejected: list[str] = []
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
            await comp.publisher.publish(sheet)
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
