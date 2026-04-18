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
from prime_jennie_runtime.slow_loop.macro.post_processor import MacroPostResult
from prime_jennie_runtime.slow_loop.scout.schemas import ScoutOutput

logger = logging.getLogger(__name__)

# Model pricing per 1M tokens (USD). upstream orchestrator 가 실제 usage 를
# RoleInvocationResult 에 넘겨주지 않아, prompt+output 길이로 거칠게 추정한다.
# 정확도 ±20% 수준. 정확 비용은 Anthropic/DeepSeek usage report 에서 확인.
_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-7": (15.0, 75.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (0.80, 4.0),
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
}


def _tier_model(tier: str) -> str | None:
    """tier → 설정된 모델명 (ANTHROPIC_MODEL / DEEPSEEK_MODEL / DEEPSEEK_SHADOW_MODEL)."""
    if tier == "reasoning":
        return os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7")
    if tier == "strong":
        return os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    if tier == "shadow_reasoning":
        return os.environ.get("DEEPSEEK_SHADOW_MODEL", "deepseek-chat")
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
    shadow_result: dict[str, Any] | None = None,
) -> None:
    """Macro 결과 1건을 macro_runs 에 upsert."""
    if engine is None:
        return
    meta = _role_metadata(macro_step_result)
    model_name = meta.get("model") or meta.get("model_used") or _tier_model("reasoning")
    # Output char count — reasoning + JSON risks 등
    out_chars = len(post.output.reasoning or "") + sum(
        len(getattr(r, "description", "") or "") for r in post.output.top_risks
    )
    cost = meta.get("cost_usd")
    if cost is None and prompt_chars is not None:
        cost = _estimate_cost(model_name, prompt_chars, out_chars)
    step = CouncilStepOutput(
        name="macro_gate",
        output={},
        model_used=model_name,
        cost_usd=cost,
        latency_ms=_role_duration_ms(macro_step_result),
        prompt_version=meta.get("prompt_version") or "v1",
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


async def persist_scout_run(
    engine: AsyncEngine | None,
    *,
    scout_run_id: str,
    generated_at: datetime,
    scout_out: ScoutOutput,
    scout_step_result: Any,
    candidates_count: int | None = None,
    prompt_chars: int | None = None,
) -> None:
    """Scout 결과 1건을 scout_runs 에 upsert."""
    if engine is None:
        return
    meta = _role_metadata(scout_step_result)
    model_name = meta.get("model") or meta.get("model_used") or _tier_model("strong")
    code_text = scout_out.screening_code
    code_hash = hashlib.sha256(code_text.encode("utf-8")).hexdigest()
    out_chars = len(code_text) + len(scout_out.hypothesis or "")
    cost = meta.get("cost_usd")
    if cost is None and prompt_chars is not None:
        cost = _estimate_cost(model_name, prompt_chars, out_chars)
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
            candidates_count, model_used, prompt_version, cost_usd, metadata_json
        ) VALUES (
            :id, :at, :hash, :code, :hyp,
            :cnt, :model, :pv, :cost, CAST(:meta AS JSONB)
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
            metadata_json = EXCLUDED.metadata_json
        """
    )
    params = {
        "id": scout_run_id,
        "at": generated_at,
        "hash": code_hash,
        "code": code_text,
        "hyp": scout_out.hypothesis,
        "cnt": candidates_count if candidates_count is not None else scout_out.expected_candidates,
        "model": model_name,
        "pv": meta.get("prompt_version") or "v1",
        "cost": cost,
        "meta": json.dumps(metadata_json, ensure_ascii=False, default=str),
    }
    try:
        async with engine.begin() as conn:
            await conn.execute(sql, params)
    except Exception:
        logger.exception("persist_scout_run failed: id=%s", scout_run_id)
