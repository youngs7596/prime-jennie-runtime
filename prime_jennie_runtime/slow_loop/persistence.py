"""Slow loop 결과를 Postgres 에 기록.

- macro_runs: `council_logging.save_council_run` 재사용 (단일-step macro_gate 도 지원)
- scout_runs: 단순 INSERT (code_text + 메타)

DB 미구성 (engine=None) 인 경우 no-op 으로 동작하여 테스트/smoke 가 깨지지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import logging
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
) -> None:
    """Macro 결과 1건을 macro_runs 에 upsert."""
    if engine is None:
        return
    meta = _role_metadata(macro_step_result)
    step = CouncilStepOutput(
        name="macro_gate",
        output={},
        model_used=meta.get("model") or meta.get("model_used"),
        cost_usd=meta.get("cost_usd"),
        latency_ms=_role_duration_ms(macro_step_result),
        prompt_version=meta.get("prompt_version"),
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


async def persist_scout_run(
    engine: AsyncEngine | None,
    *,
    scout_run_id: str,
    generated_at: datetime,
    scout_out: ScoutOutput,
    scout_step_result: Any,
    candidates_count: int | None = None,
) -> None:
    """Scout 결과 1건을 scout_runs 에 upsert."""
    if engine is None:
        return
    meta = _role_metadata(scout_step_result)
    code_text = scout_out.screening_code
    code_hash = hashlib.sha256(code_text.encode("utf-8")).hexdigest()
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
        "model": meta.get("model") or meta.get("model_used"),
        "pv": meta.get("prompt_version"),
        "cost": meta.get("cost_usd"),
        "meta": json.dumps(metadata_json, ensure_ascii=False, default=str),
    }
    try:
        async with engine.begin() as conn:
            await conn.execute(sql, params)
    except Exception:
        logger.exception("persist_scout_run failed: id=%s", scout_run_id)
