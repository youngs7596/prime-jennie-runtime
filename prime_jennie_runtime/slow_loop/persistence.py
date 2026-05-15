"""Slow loop 결과를 Postgres 에 기록.

- macro_runs: `council_logging.save_council_run` 재사용 (단일-step macro_gate 도 지원)
- scout_runs: 단순 INSERT (code_text + 메타)

DB 미구성 (engine=None) 인 경우 no-op 으로 동작하여 테스트/smoke 가 깨지지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from prime_jennie_runtime.council_logging import (
    CouncilRunRecord,
    CouncilStepOutput,
    save_council_run,
)
from prime_jennie_runtime.infra.llm_stats import record_llm_call
from prime_jennie_runtime.slow_loop.macro.post_processor import MacroPostResult
from prime_jennie_runtime.slow_loop.scout.schemas import ScoutOutput, ScreeningCandidate

logger = logging.getLogger(__name__)

# Model pricing per 1M tokens (USD).
# minyoung-mah >= 0.1.2 는 RoleInvocationResult.metadata["usage"] 에 실측 token 수
# (input/output/total) 를 싣는다 — Anthropic/OpenAI/DeepSeek 표준 경로. 이 경우
# _real_cost 로 정확 비용 계산. 0.1.1 이하 provider 거나 mock 인 경우 prompt+output
# 문자수 기반 추정(±20%) 으로 fallback.
_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-7": (15.0, 75.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (0.80, 4.0),
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
}


def _tier_model(tier: str) -> str | None:
    """tier → 설정된 모델명 (DEEPSEEK_MODEL / ANTHROPIC_MODEL).

    2026-04-22: Opus 비용 부담으로 primary 를 DeepSeek 로 복귀, Opus 는 shadow
    회귀 검증용. tier 매핑은 app.py `_try_build_tiered_router` 와 1:1 동기.
    """
    if tier in ("reasoning", "strong"):
        return os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    if tier in ("shadow_reasoning", "shadow_strong"):
        return os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7")
    return None


def _estimate_tokens(text_len_chars: int) -> int:
    """한국어/영어 혼재 텍스트 토큰 추정. ~2자당 1 토큰 (rough)."""
    return max(1, text_len_chars // 2)


def _estimate_cost(model: str | None, input_chars: int, output_chars: int) -> float | None:
    if model is None:
        return None
    rates = _PRICING.get(model)
    if rates is None:
        return None
    input_tok = _estimate_tokens(input_chars)
    output_tok = _estimate_tokens(output_chars)
    return input_tok * rates[0] / 1e6 + output_tok * rates[1] / 1e6


def _real_cost(model: str | None, usage: dict[str, Any]) -> float | None:
    """minyoung-mah 0.1.2+ usage metadata 로 정확 비용 계산."""
    if model is None:
        return None
    rates = _PRICING.get(model)
    if rates is None:
        return None
    input_tok = usage.get("input_tokens")
    output_tok = usage.get("output_tokens")
    if not isinstance(input_tok, int) or not isinstance(output_tok, int):
        return None
    return input_tok * rates[0] / 1e6 + output_tok * rates[1] / 1e6


def _resolve_cost(
    meta: dict[str, Any], model: str | None, prompt_chars: int | None, output_chars: int
) -> float | None:
    """cost 우선순위: meta[cost_usd] 직접값 → meta[usage] 실측 → char 기반 추정."""
    if meta.get("cost_usd") is not None:
        return meta["cost_usd"]
    usage = meta.get("usage")
    if isinstance(usage, dict):
        cost = _real_cost(model, usage)
        if cost is not None:
            return cost
    if prompt_chars is not None:
        return _estimate_cost(model, prompt_chars, output_chars)
    return None


def _usage_tokens(
    meta: dict[str, Any],
    prompt_chars: int | None,
    output_chars: int,
) -> tuple[int, int]:
    """(input_tokens, output_tokens) 추정. meta[usage] 실측이 있으면 우선."""
    usage = meta.get("usage")
    if isinstance(usage, dict):
        in_tok = usage.get("input_tokens")
        out_tok = usage.get("output_tokens")
        if isinstance(in_tok, int) and isinstance(out_tok, int):
            return in_tok, out_tok
    in_est = _estimate_tokens(prompt_chars) if prompt_chars is not None else 0
    out_est = _estimate_tokens(output_chars)
    return in_est, out_est


def _role_metadata(pipeline_step_result: Any) -> dict[str, Any]:
    """PipelineStepResult.outputs[0].metadata 추출 (model/cost 등)."""
    try:
        outputs = pipeline_step_result.outputs
        if outputs:
            return dict(outputs[0].metadata or {})
    except Exception:
        pass
    return {}


def _role_duration_ms(pipeline_step_result: Any) -> int | None:
    try:
        outputs = pipeline_step_result.outputs
        if outputs:
            return int(outputs[0].duration_ms)
    except Exception:
        pass
    return None


async def persist_macro_run(
    engine: AsyncEngine | None,
    *,
    macro_run_id: str,
    generated_at: datetime,
    trigger_reason: str,
    post: MacroPostResult,
    macro_step_result: Any,
    news_digest_ref: str | None = None,
    prompt_chars: int | None = None,
    prompt_version: str | None = None,
    shadow_result: dict[str, Any] | None = None,
    redis_client: Any = None,
) -> None:
    """Macro 결과 1건을 macro_runs 에 upsert."""
    meta = _role_metadata(macro_step_result)
    model_name = meta.get("model") or meta.get("model_used") or _tier_model("reasoning")
    # Output char count — reasoning + JSON risks 등
    out_chars = len(post.output.reasoning or "") + sum(
        len(getattr(r, "description", "") or "") for r in post.output.top_risks
    )
    cost = _resolve_cost(meta, model_name, prompt_chars, out_chars)
    if engine is None:
        await _record_macro_llm_stats(
            redis_client, meta, prompt_chars, out_chars, cost, shadow_result, generated_at
        )
        return
    # Prompt 버전 우선순위: caller 명시 > role metadata > default
    resolved_prompt_version = prompt_version or meta.get("prompt_version") or "v1"
    step = CouncilStepOutput(
        name="macro_gate",
        output={},
        model_used=model_name,
        cost_usd=cost,
        latency_ms=_role_duration_ms(macro_step_result),
        prompt_version=resolved_prompt_version,
    )
    record = CouncilRunRecord(
        macro_run_id=macro_run_id,
        generated_at=generated_at,
        trigger_reason=trigger_reason,
        gate=post.output.gate,
        size_multiplier=float(post.output.size_multiplier),
        reasoning=post.output.reasoning,
        top_risks=[
            r.model_dump() if hasattr(r, "model_dump") else dict(r) for r in post.output.top_risks
        ],
        confidence=post.output.confidence,
        news_digest_ref=news_digest_ref,
        next_review_hint=getattr(post.output, "next_review_hint", None),
        prompt_version=step.prompt_version,
        model_used=step.model_used,
        cost_usd=step.cost_usd,
        latency_ms=step.latency_ms,
        auto_override=bool(getattr(post, "auto_override_applied", False)),
        steps=[step],
    )
    try:
        await save_council_run(engine, record)
    except Exception:
        logger.exception("persist_macro_run failed: id=%s", macro_run_id)
        return
    # Shadow result merge into metadata_json.shadow
    if shadow_result is not None:
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE macro_runs SET metadata_json = "
                        "COALESCE(metadata_json, '{}'::jsonb) || CAST(:shadow AS JSONB) "
                        "WHERE macro_run_id = :id"
                    ),
                    {
                        "id": macro_run_id,
                        "shadow": json.dumps(
                            {"shadow": shadow_result}, ensure_ascii=False, default=str
                        ),
                    },
                )
        except Exception:
            logger.exception("persist_macro_run shadow merge failed: id=%s", macro_run_id)

    await _record_macro_llm_stats(
        redis_client, meta, prompt_chars, out_chars, cost, shadow_result, generated_at
    )


async def _record_macro_llm_stats(
    redis_client: Any,
    meta: dict[str, Any],
    prompt_chars: int | None,
    out_chars: int,
    cost: float | None,
    shadow_result: dict[str, Any] | None,
    generated_at: datetime,
) -> None:
    """Macro + (optional) shadow 사용량을 Redis 에 누적."""
    if redis_client is None:
        return
    in_tok, out_tok = _usage_tokens(meta, prompt_chars, out_chars)
    await record_llm_call(
        redis_client,
        service="macro",
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=cost,
        timestamp=generated_at,
    )
    if shadow_result is None or "error" in shadow_result:
        return
    shadow_cost = shadow_result.get("cost_usd_estimated")
    shadow_in = shadow_result.get("tokens_in")
    shadow_out = shadow_result.get("tokens_out")
    if not isinstance(shadow_in, int) or not isinstance(shadow_out, int):
        # Shadow pipeline 은 별도 usage metadata 를 payload 에 싣지 않아 primary 기준 추정.
        shadow_in, shadow_out = in_tok, out_tok
    await record_llm_call(
        redis_client,
        service="macro_shadow",
        input_tokens=shadow_in,
        output_tokens=shadow_out,
        cost_usd=shadow_cost,
        timestamp=generated_at,
    )


async def persist_scout_run(
    engine: AsyncEngine | None,
    *,
    scout_run_id: str,
    generated_at: datetime,
    scout_out: ScoutOutput,
    scout_step_result: Any,
    candidates_count: int | None = None,
    prompt_chars: int | None = None,
    prompt_version: str | None = None,
    context_snapshot: dict[str, Any] | None = None,
    shadow_result: dict[str, Any] | None = None,
    redis_client: Any = None,
) -> None:
    """Scout 결과 1건을 scout_runs 에 upsert.

    context_snapshot 은 Scout 코드 실행 입력(news_events, sector_momentum,
    macro_size_multiplier, universe_hash 등)의 생성 시점 스냅샷이다. 백테스트
    재현 시 같은 코드 × 같은 입력을 복원하는 primary key — migration 012 의
    scout_runs.context_snapshot_json 컬럼.
    """
    meta = _role_metadata(scout_step_result)
    model_name = meta.get("model") or meta.get("model_used") or _tier_model("strong")
    code_text = scout_out.screening_code
    code_hash = hashlib.sha256(code_text.encode("utf-8")).hexdigest()
    out_chars = len(code_text) + len(scout_out.hypothesis or "")
    cost = _resolve_cost(meta, model_name, prompt_chars, out_chars)
    if engine is None:
        if redis_client is not None:
            in_tok, out_tok = _usage_tokens(meta, prompt_chars, out_chars)
            await record_llm_call(
                redis_client,
                service="scout",
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_usd=cost,
                timestamp=generated_at,
            )
        return
    metadata_json = {
        "hypothesis": scout_out.hypothesis,
        "factor_weights": scout_out.factor_weights,
        "strategy_tags_used": scout_out.strategy_tags_used,
        "fallback_strategy": scout_out.fallback_strategy,
        "estimated_runtime_seconds": scout_out.estimated_runtime_seconds,
        "duration_ms": _role_duration_ms(scout_step_result),
    }
    sql = text(
        """
        INSERT INTO scout_runs (
            scout_run_id, generated_at, code_hash, code_text, hypothesis,
            candidates_count, model_used, prompt_version, cost_usd, metadata_json,
            context_snapshot_json
        ) VALUES (
            :id, :at, :hash, :code, :hyp,
            :cnt, :model, :pv, :cost, CAST(:meta AS JSONB),
            CAST(:ctx AS JSONB)
        )
        ON CONFLICT (scout_run_id) DO UPDATE SET
            generated_at = EXCLUDED.generated_at,
            code_hash = EXCLUDED.code_hash,
            code_text = EXCLUDED.code_text,
            hypothesis = EXCLUDED.hypothesis,
            candidates_count = EXCLUDED.candidates_count,
            model_used = EXCLUDED.model_used,
            prompt_version = EXCLUDED.prompt_version,
            cost_usd = EXCLUDED.cost_usd,
            metadata_json = EXCLUDED.metadata_json,
            context_snapshot_json = EXCLUDED.context_snapshot_json
        """
    )
    resolved_prompt_version = prompt_version or meta.get("prompt_version") or "v1"
    params = {
        "id": scout_run_id,
        "at": generated_at,
        "hash": code_hash,
        "code": code_text,
        "hyp": scout_out.hypothesis,
        "cnt": candidates_count,
        "model": model_name,
        "pv": resolved_prompt_version,
        "cost": cost,
        "meta": json.dumps(metadata_json, ensure_ascii=False, default=str),
        "ctx": json.dumps(context_snapshot or {}, ensure_ascii=False, default=str),
    }
    try:
        async with engine.begin() as conn:
            await conn.execute(sql, params)
    except Exception:
        logger.exception("persist_scout_run failed: id=%s", scout_run_id)
        return

    # Shadow result merge into metadata_json.shadow (macro 와 동일 패턴)
    if shadow_result is not None:
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE scout_runs SET metadata_json = "
                        "COALESCE(metadata_json, '{}'::jsonb) || CAST(:shadow AS JSONB) "
                        "WHERE scout_run_id = :id"
                    ),
                    {
                        "id": scout_run_id,
                        "shadow": json.dumps(
                            {"shadow": shadow_result}, ensure_ascii=False, default=str
                        ),
                    },
                )
        except Exception:
            logger.exception("persist_scout_run shadow merge failed: id=%s", scout_run_id)

    if redis_client is not None:
        in_tok, out_tok = _usage_tokens(meta, prompt_chars, out_chars)
        await record_llm_call(
            redis_client,
            service="scout",
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
            timestamp=generated_at,
        )
        # Shadow llm_stats 누적 (macro_shadow 와 대칭)
        if shadow_result is not None and "error" not in shadow_result:
            shadow_cost = shadow_result.get("cost_usd_estimated")
            shadow_in = shadow_result.get("tokens_in")
            shadow_out = shadow_result.get("tokens_out")
            if not isinstance(shadow_in, int) or not isinstance(shadow_out, int):
                # Shadow pipeline 은 별도 usage metadata 를 payload 에 싣지 않아 primary 기준 추정.
                shadow_in, shadow_out = in_tok, out_tok
            await record_llm_call(
                redis_client,
                service="scout_shadow",
                input_tokens=shadow_in,
                output_tokens=shadow_out,
                cost_usd=shadow_cost,
                timestamp=generated_at,
            )


async def persist_screening_candidates(
    engine: AsyncEngine | None,
    *,
    scout_run_id: str,
    candidates: list[ScreeningCandidate],
) -> None:
    """Scout 코드가 반환한 raw 후보 전수를 screening_candidates 에 일괄 INSERT.

    promoted_to_sheet_id / rejection_reason 은 이후 Strategy Engine 결정 시점에
    update_candidate_promotion 이 채운다 — 여기선 NULL. rank 는 반환 순서
    (0-indexed). 동일 scout_run_id 로 재실행되면 기존 row 는 DELETE 후 재삽입.
    """
    if engine is None:
        return
    if not candidates:
        return
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM screening_candidates WHERE scout_run_id = :id"),
                {"id": scout_run_id},
            )
            for rank, cand in enumerate(candidates):
                await conn.execute(
                    text(
                        """
                        INSERT INTO screening_candidates (
                            scout_run_id, rank, ticker, strategy_tag, conviction,
                            entry_hint_json, exit_hint_json, factors_json, notes
                        ) VALUES (
                            :sid, :rank, :ticker, :tag, :conv,
                            CAST(:entry AS JSONB),
                            CAST(:exit AS JSONB),
                            CAST(:factors AS JSONB),
                            :notes
                        )
                        """
                    ),
                    {
                        "sid": scout_run_id,
                        "rank": rank,
                        "ticker": cand.ticker,
                        "tag": cand.strategy_tag,
                        "conv": float(cand.conviction),
                        "entry": json.dumps(
                            cand.entry_hint.model_dump(), ensure_ascii=False, default=str
                        ),
                        "exit": (
                            json.dumps(cand.exit_hint.model_dump(), ensure_ascii=False, default=str)
                            if cand.exit_hint is not None
                            else None
                        ),
                        "factors": json.dumps(cand.factors, ensure_ascii=False, default=str),
                        "notes": cand.notes or "",
                    },
                )
    except Exception:
        logger.exception("persist_screening_candidates failed: id=%s", scout_run_id)


async def fetch_previous_run_candidates(
    engine: AsyncEngine | None,
    *,
    as_of: datetime,
    macro_gate: str,
    macro_size_multiplier: float,
    max_age_hours: int = 24,
    size_tolerance: float = 0.05,
) -> tuple[str, list[ScreeningCandidate]] | None:
    """직전 scout_run 의 raw candidates 를 재사용 가능 여부 점검 후 반환.

    조건 (스펙: use_previous_run fallback):
    1. 24h 이내 (`as_of - max_age_hours <= generated_at < as_of`)
    2. 같은 거래일 macro_state 하 — `macro_gate` 일치 AND
       `|macro_size_multiplier 차이| <= size_tolerance`
    3. candidates_count > 0 (성공한 run)

    가장 최신 1건만 반환. 매칭 없으면 None.

    DB 미구성 또는 SQL 실패 시 None — 호출자가 `skip_today` 로 fallback.
    """
    if engine is None:
        return None
    from datetime import timedelta

    since = as_of - timedelta(hours=max_age_hours)
    try:
        async with engine.begin() as conn:
            res = await conn.execute(
                text(
                    """
                    SELECT scout_run_id,
                           metadata_json->'fallback_macro_gate'        AS prev_gate,
                           metadata_json->'fallback_macro_size'        AS prev_size,
                           context_snapshot_json->>'macro_gate'        AS ctx_gate,
                           context_snapshot_json->>'macro_size_multiplier' AS ctx_size,
                           generated_at
                    FROM scout_runs
                    WHERE generated_at >= :since
                      AND generated_at < :now
                      AND candidates_count > 0
                    ORDER BY generated_at DESC
                    LIMIT 10
                    """
                ),
                {"since": since, "now": as_of},
            )
            rows = res.mappings().all()
    except Exception:
        logger.exception("fetch_previous_run_candidates: query failed")
        return None

    chosen_id: str | None = None
    for r in rows:
        # ctx_gate / ctx_size 는 context_snapshot_json 에서 추출 (Phase 2.10 신설).
        ctx_gate = r["ctx_gate"]
        ctx_size_raw = r["ctx_size"]
        if ctx_gate is None or ctx_size_raw is None:
            continue
        try:
            ctx_size = float(ctx_size_raw)
        except (TypeError, ValueError):
            continue
        if ctx_gate != macro_gate:
            continue
        if abs(ctx_size - macro_size_multiplier) > size_tolerance:
            continue
        chosen_id = r["scout_run_id"]
        break

    if chosen_id is None:
        return None

    # candidates 재구성
    try:
        async with engine.begin() as conn:
            res = await conn.execute(
                text(
                    """
                    SELECT ticker, strategy_tag, conviction,
                           entry_hint_json, exit_hint_json, factors_json, notes
                    FROM screening_candidates
                    WHERE scout_run_id = :sid
                    ORDER BY rank
                    """
                ),
                {"sid": chosen_id},
            )
            cand_rows = res.mappings().all()
    except Exception:
        logger.exception("fetch_previous_run_candidates: candidates query failed")
        return None

    candidates: list[ScreeningCandidate] = []
    from .scout.schemas import EntryHint, ExitHint  # local import to avoid cycles

    for r in cand_rows:
        try:
            entry_raw = r["entry_hint_json"]
            entry = EntryHint.model_validate(
                entry_raw if isinstance(entry_raw, dict) else json.loads(entry_raw or "{}")
            )
            exit_raw = r["exit_hint_json"]
            exit_hint: ExitHint | None = None
            if exit_raw is not None:
                exit_dict = exit_raw if isinstance(exit_raw, dict) else json.loads(exit_raw)
                if exit_dict:
                    exit_hint = ExitHint.model_validate(exit_dict)
            factors_raw = r["factors_json"]
            factors = (
                factors_raw if isinstance(factors_raw, dict) else json.loads(factors_raw or "{}")
            )
            candidates.append(
                ScreeningCandidate(
                    ticker=r["ticker"],
                    strategy_tag=r["strategy_tag"],
                    conviction=float(r["conviction"] or 0.0),
                    entry_hint=entry,
                    exit_hint=exit_hint,
                    factors=factors,
                    notes=r["notes"] or "",
                )
            )
        except Exception:
            logger.warning(
                "fetch_previous_run_candidates: skip malformed candidate sid=%s ticker=%s",
                chosen_id,
                r.get("ticker"),
            )
            continue

    if not candidates:
        return None
    return chosen_id, candidates


async def update_candidate_promotion(
    engine: AsyncEngine | None,
    *,
    scout_run_id: str,
    ticker: str,
    sheet_id: str | None = None,
    rejection_reason: str | None = None,
) -> None:
    """Strategy Engine 결정 반영. sheet_id 또는 rejection_reason 둘 중 하나.

    같은 scout_run 내에서 동일 ticker 가 여러 rank 로 등장하는 경우 전부 업데이트
    한다 (거의 없지만 LLM 출력이 우회적으로 중복할 수 있음).
    """
    if engine is None:
        return
    if sheet_id is None and rejection_reason is None:
        return
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    UPDATE screening_candidates
                    SET promoted_to_sheet_id = :sid,
                        rejection_reason = :reason
                    WHERE scout_run_id = :run AND ticker = :ticker
                    """
                ),
                {
                    "run": scout_run_id,
                    "ticker": ticker,
                    "sid": sheet_id,
                    "reason": rejection_reason,
                },
            )
    except Exception:
        logger.exception(
            "update_candidate_promotion failed: run=%s ticker=%s", scout_run_id, ticker
        )
