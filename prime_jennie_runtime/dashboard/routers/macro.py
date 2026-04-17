"""Macro API — 매크로 게이트 실행 이력 조회.

v2 원본: `prime_jennie/services/dashboard/routers/macro.py`

v3 매핑: v2 `daily_macro_insights` → v3 `macro_runs` (migrations/001).
v3 스키마는 v2 대비 단순화(binary gate + size_multiplier + reasoning + JSON 메타).
v2 의 sector_signals/sentiment_score 등은 `metadata_json` 안에 보존될 수 있어,
기본 필드 + metadata 를 그대로 내려준다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import get_session

router = APIRouter(prefix="/macro", tags=["macro"])


class MacroRun(BaseModel):
    macro_run_id: str
    generated_at: datetime
    trigger_reason: str
    gate: str  # "open" | "closed"
    size_multiplier: float
    reasoning: str | None = None
    top_risks: list[Any] = []
    confidence: str | None = None
    news_digest_ref: str | None = None
    next_review_hint: str | None = None
    prompt_version: str | None = None
    model_used: str | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    auto_override: bool = False
    metadata: dict[str, Any] = {}


class RegimeResponse(BaseModel):
    """현재 macro 상태 요약 — v3 gate binary + size_multiplier."""

    gate: str
    size_multiplier: float
    confidence: str | None = None
    generated_at: datetime | None = None


def _row_to_run(row: Any) -> MacroRun:
    def _as_dict(v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, (dict, list)):
            return v
        if isinstance(v, str):
            import json

            return json.loads(v)
        return v

    return MacroRun(
        macro_run_id=row["macro_run_id"],
        generated_at=row["generated_at"],
        trigger_reason=row["trigger_reason"],
        gate=row["gate"],
        size_multiplier=float(row["size_multiplier"]),
        reasoning=row["reasoning"],
        top_risks=_as_dict(row["top_risks_json"]) or [],
        confidence=row["confidence"],
        news_digest_ref=row["news_digest_ref"],
        next_review_hint=row["next_review_hint"],
        prompt_version=row["prompt_version"],
        model_used=row["model_used"],
        cost_usd=float(row["cost_usd"]) if row["cost_usd"] is not None else None,
        latency_ms=row["latency_ms"],
        auto_override=bool(row["auto_override"]),
        metadata=_as_dict(row["metadata_json"]) or {},
    )


@router.get("/insight", response_model=MacroRun | dict)
async def get_insight(
    target_date: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> MacroRun | dict:
    """최신 또는 특정 날짜의 매크로 run. 해당 날짜 데이터 없으면 `{status: "no_data"}`."""
    if target_date:
        day = datetime.fromisoformat(target_date).replace(tzinfo=UTC)
        result = await session.execute(
            text(
                "SELECT * FROM macro_runs "
                "WHERE generated_at >= :start AND generated_at < :end "
                "ORDER BY generated_at DESC LIMIT 1"
            ),
            {"start": day, "end": day + timedelta(days=1)},
        )
    else:
        result = await session.execute(
            text("SELECT * FROM macro_runs ORDER BY generated_at DESC LIMIT 1")
        )
    row = result.mappings().one_or_none()
    if row is None:
        return {"status": "no_data"}
    return _row_to_run(row)


@router.get("/regime", response_model=RegimeResponse)
async def get_regime(session: AsyncSession = Depends(get_session)) -> RegimeResponse:
    """최신 macro run 기반 현재 게이트 상태."""
    result = await session.execute(
        text(
            "SELECT gate, size_multiplier, confidence, generated_at FROM macro_runs "
            "ORDER BY generated_at DESC LIMIT 1"
        )
    )
    row = result.mappings().one_or_none()
    if row is None:
        return RegimeResponse(gate="closed", size_multiplier=0.0)
    return RegimeResponse(
        gate=row["gate"],
        size_multiplier=float(row["size_multiplier"]),
        confidence=row["confidence"],
        generated_at=row["generated_at"],
    )


@router.get("/dates", response_model=list[str])
async def get_dates(
    limit: int = 30,
    session: AsyncSession = Depends(get_session),
) -> list[str]:
    """macro_runs 가 있는 날짜 목록 (최근순)."""
    result = await session.execute(
        text(
            "SELECT DISTINCT DATE(generated_at AT TIME ZONE 'Asia/Seoul') AS d "
            "FROM macro_runs ORDER BY d DESC LIMIT :limit"
        ),
        {"limit": max(1, min(limit, 365))},
    )
    return [row[0].isoformat() for row in result.all()]


@router.get("/runs", response_model=list[MacroRun])
async def list_runs(
    limit: int = 20,
    session: AsyncSession = Depends(get_session),
) -> list[MacroRun]:
    """최근 macro run 페이징 없이 limit 개."""
    result = await session.execute(
        text("SELECT * FROM macro_runs ORDER BY generated_at DESC LIMIT :limit"),
        {"limit": max(1, min(limit, 200))},
    )
    return [_row_to_run(r) for r in result.mappings().all()]


@router.get("/runs/{macro_run_id}", response_model=MacroRun)
async def get_run(macro_run_id: str, session: AsyncSession = Depends(get_session)) -> MacroRun:
    result = await session.execute(
        text("SELECT * FROM macro_runs WHERE macro_run_id = :id"),
        {"id": macro_run_id},
    )
    row = result.mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"macro run not found: {macro_run_id}")
    return _row_to_run(row)
