"""macro_runs 테이블 기반 Council 로그 영속/조회.

신규 마이그레이션 없이 `macro_runs.metadata_json` JSONB 에 확장 필드를 병합 저장.
v3 `slow_loop/macro/` 가 기존에 쓰는 필드(`gate`, `size_multiplier`, `reasoning`,
`top_risks_json`, `confidence`, `news_digest_ref` 등)는 컬럼으로, v2 council 3-step
및 replay 용 입력은 `metadata_json.council_log` 하위에 격납.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .records import CouncilRunRecord, CouncilStepOutput

_METADATA_KEY = "council_log"


def _record_to_metadata_payload(record: CouncilRunRecord) -> dict[str, Any]:
    """레코드의 확장 필드만 metadata_json 하위로 직렬화."""
    return {
        "target_date": record.target_date.isoformat() if record.target_date else None,
        "steps": [
            {
                "name": s.name,
                "output": s.output,
                "model_used": s.model_used,
                "cost_usd": s.cost_usd,
                "latency_ms": s.latency_ms,
                "prompt_version": s.prompt_version,
            }
            for s in record.steps
        ],
        "council_consensus": record.council_consensus,
        "trading_reasoning": record.trading_reasoning,
        "strategies_to_favor": record.strategies_to_favor,
        "strategies_to_avoid": record.strategies_to_avoid,
        "sectors_to_favor": record.sectors_to_favor,
        "sectors_to_avoid": record.sectors_to_avoid,
        "opportunity_factors": record.opportunity_factors,
        "risk_factors": record.risk_factors,
        "input": {
            "briefing_text": record.input_briefing_text,
            "global_snapshot": record.input_global_snapshot,
            "political_news": record.input_political_news,
            "sector_momentum_text": record.input_sector_momentum_text,
            "index_technical_text": record.input_index_technical_text,
        },
        "final_sentiment": record.final_sentiment,
        "final_sentiment_score": record.final_sentiment_score,
        "final_regime_hint": record.final_regime_hint,
        "final_position_size_pct": record.final_position_size_pct,
        "final_stop_loss_adjust_pct": record.final_stop_loss_adjust_pct,
        "political_risk_level": record.political_risk_level,
        "success": record.success,
        "error": record.error,
    }


def _metadata_payload_to_record_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    """metadata_json.council_log → CouncilRunRecord 확장 필드 kwargs."""
    if not payload:
        return {}
    steps = [
        CouncilStepOutput(
            name=s.get("name", ""),
            output=s.get("output", {}) or {},
            model_used=s.get("model_used"),
            cost_usd=s.get("cost_usd"),
            latency_ms=s.get("latency_ms"),
            prompt_version=s.get("prompt_version"),
        )
        for s in payload.get("steps", []) or []
    ]
    target_date_str = payload.get("target_date")
    target_date_val: date | None
    if target_date_str:
        try:
            target_date_val = date.fromisoformat(target_date_str)
        except ValueError:
            target_date_val = None
    else:
        target_date_val = None
    input_data = payload.get("input", {}) or {}
    return {
        "target_date": target_date_val,
        "steps": steps,
        "council_consensus": payload.get("council_consensus"),
        "trading_reasoning": payload.get("trading_reasoning", "") or "",
        "strategies_to_favor": payload.get("strategies_to_favor", []) or [],
        "strategies_to_avoid": payload.get("strategies_to_avoid", []) or [],
        "sectors_to_favor": payload.get("sectors_to_favor", []) or [],
        "sectors_to_avoid": payload.get("sectors_to_avoid", []) or [],
        "opportunity_factors": payload.get("opportunity_factors", []) or [],
        "risk_factors": payload.get("risk_factors", []) or [],
        "input_briefing_text": input_data.get("briefing_text", "") or "",
        "input_global_snapshot": input_data.get("global_snapshot", {}) or {},
        "input_political_news": input_data.get("political_news", []) or [],
        "input_sector_momentum_text": input_data.get("sector_momentum_text", "") or "",
        "input_index_technical_text": input_data.get("index_technical_text", "") or "",
        "final_sentiment": payload.get("final_sentiment"),
        "final_sentiment_score": payload.get("final_sentiment_score"),
        "final_regime_hint": payload.get("final_regime_hint"),
        "final_position_size_pct": payload.get("final_position_size_pct"),
        "final_stop_loss_adjust_pct": payload.get("final_stop_loss_adjust_pct"),
        "political_risk_level": payload.get("political_risk_level"),
        "success": bool(payload.get("success", True)),
        "error": payload.get("error", "") or "",
    }


def _row_to_record(row: Any) -> CouncilRunRecord:
    """DB row → CouncilRunRecord."""
    raw_meta = row.metadata_json or {}
    if isinstance(raw_meta, str):
        try:
            raw_meta = json.loads(raw_meta)
        except json.JSONDecodeError:
            raw_meta = {}
    council_meta = raw_meta.get(_METADATA_KEY, {}) if isinstance(raw_meta, dict) else {}

    top_risks = row.top_risks_json or []
    if isinstance(top_risks, str):
        try:
            top_risks = json.loads(top_risks)
        except json.JSONDecodeError:
            top_risks = []

    record = CouncilRunRecord(
        macro_run_id=row.macro_run_id,
        generated_at=row.generated_at,
        trigger_reason=row.trigger_reason,
        gate=row.gate,
        size_multiplier=float(row.size_multiplier),
        reasoning=row.reasoning,
        top_risks=top_risks if isinstance(top_risks, list) else [],
        confidence=row.confidence,
        news_digest_ref=row.news_digest_ref,
        next_review_hint=row.next_review_hint,
        prompt_version=row.prompt_version,
        model_used=row.model_used,
        cost_usd=float(row.cost_usd) if row.cost_usd is not None else None,
        latency_ms=row.latency_ms,
        auto_override=bool(row.auto_override),
        **_metadata_payload_to_record_kwargs(council_meta),
    )
    return record


async def save_council_run(engine: AsyncEngine, record: CouncilRunRecord) -> None:
    """Council 실행 1건을 macro_runs 에 upsert.

    - 기존 macro_runs 컬럼에는 주요 필드(gate/size_multiplier/reasoning/...) 만 기록
    - 확장 필드(3-step 출력, 입력 컨텍스트, 전략/섹터 목록) 는 metadata_json.council_log 에
    - 같은 macro_run_id 가 이미 있으면 덮어쓰기 (ON CONFLICT DO UPDATE)
    """
    council_payload = _record_to_metadata_payload(record)
    # 기존 metadata_json 과 병합 (upsert 시 덮어쓰되 council_log 만 교체)
    metadata_json = {_METADATA_KEY: council_payload}

    sql = text(
        """
        INSERT INTO macro_runs (
            macro_run_id, generated_at, trigger_reason, gate, size_multiplier,
            reasoning, top_risks_json, confidence, news_digest_ref,
            next_review_hint, prompt_version, model_used, cost_usd, latency_ms,
            auto_override, metadata_json
        ) VALUES (
            :macro_run_id, :generated_at, :trigger_reason, :gate, :size_multiplier,
            :reasoning, CAST(:top_risks_json AS JSONB), :confidence, :news_digest_ref,
            :next_review_hint, :prompt_version, :model_used, :cost_usd, :latency_ms,
            :auto_override, CAST(:metadata_json AS JSONB)
        )
        ON CONFLICT (macro_run_id) DO UPDATE SET
            generated_at = EXCLUDED.generated_at,
            trigger_reason = EXCLUDED.trigger_reason,
            gate = EXCLUDED.gate,
            size_multiplier = EXCLUDED.size_multiplier,
            reasoning = EXCLUDED.reasoning,
            top_risks_json = EXCLUDED.top_risks_json,
            confidence = EXCLUDED.confidence,
            news_digest_ref = EXCLUDED.news_digest_ref,
            next_review_hint = EXCLUDED.next_review_hint,
            prompt_version = EXCLUDED.prompt_version,
            model_used = EXCLUDED.model_used,
            cost_usd = EXCLUDED.cost_usd,
            latency_ms = EXCLUDED.latency_ms,
            auto_override = EXCLUDED.auto_override,
            metadata_json = COALESCE(macro_runs.metadata_json, '{}'::jsonb)
                || CAST(:metadata_json AS JSONB)
        """
    )
    params = {
        "macro_run_id": record.macro_run_id,
        "generated_at": record.generated_at,
        "trigger_reason": record.trigger_reason,
        "gate": record.gate,
        "size_multiplier": record.size_multiplier,
        "reasoning": record.reasoning,
        "top_risks_json": json.dumps(record.top_risks, ensure_ascii=False),
        "confidence": record.confidence,
        "news_digest_ref": record.news_digest_ref,
        "next_review_hint": record.next_review_hint,
        "prompt_version": record.prompt_version,
        "model_used": record.model_used,
        "cost_usd": record.cost_usd,
        "latency_ms": record.latency_ms,
        "auto_override": record.auto_override,
        "metadata_json": json.dumps(metadata_json, ensure_ascii=False, default=str),
    }
    async with engine.begin() as conn:
        await conn.execute(sql, params)


async def fetch_council_run(engine: AsyncEngine, macro_run_id: str) -> CouncilRunRecord | None:
    """macro_run_id 로 단일 Council 런 조회."""
    sql = text(
        """
        SELECT macro_run_id, generated_at, trigger_reason, gate, size_multiplier,
               reasoning, top_risks_json, confidence, news_digest_ref,
               next_review_hint, prompt_version, model_used, cost_usd, latency_ms,
               auto_override, metadata_json
        FROM macro_runs
        WHERE macro_run_id = :macro_run_id
        """
    )
    async with engine.connect() as conn:
        result = await conn.execute(sql, {"macro_run_id": macro_run_id})
        row = result.one_or_none()
    if row is None:
        return None
    return _row_to_record(row)


async def list_council_runs(
    engine: AsyncEngine,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 100,
) -> list[CouncilRunRecord]:
    """기간별 Council 런 목록 (최신순)."""
    clauses = []
    params: dict[str, Any] = {"limit": int(limit)}
    if since is not None:
        clauses.append("generated_at >= :since")
        params["since"] = since
    if until is not None:
        clauses.append("generated_at <= :until")
        params["until"] = until
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = text(
        f"""
        SELECT macro_run_id, generated_at, trigger_reason, gate, size_multiplier,
               reasoning, top_risks_json, confidence, news_digest_ref,
               next_review_hint, prompt_version, model_used, cost_usd, latency_ms,
               auto_override, metadata_json
        FROM macro_runs
        {where}
        ORDER BY generated_at DESC
        LIMIT :limit
        """
    )
    async with engine.connect() as conn:
        result = await conn.execute(sql, params)
        rows = result.all()
    return [_row_to_record(r) for r in rows]


def record_as_dict(record: CouncilRunRecord) -> dict[str, Any]:
    """디버그/dashboard JSON 응답 편의용."""
    d = asdict(record)
    # datetime/date 를 isoformat 문자열로
    if record.generated_at is not None:
        d["generated_at"] = record.generated_at.isoformat()
    if record.target_date is not None:
        d["target_date"] = record.target_date.isoformat()
    return d
