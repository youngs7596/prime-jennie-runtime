"""Scout API — Scout run 이력 조회.

v3 `scout_runs` 테이블 (migrations/001) 을 대상으로 한다.

엔드포인트:
- GET /scout/runs            — 최근 runs 요약 (code_text 제외, 용량 큼)
- GET /scout/runs/{id}       — 단일 run 상세 (code_text 포함)
- GET /scout/dates           — scout_runs 가 있는 날짜 목록
- GET /scout/latest          — 최신 run 요약 (control-ui Overview 대응)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import get_session

router = APIRouter(prefix="/scout", tags=["scout"])


class ScoutRunSummary(BaseModel):
    """code_text 제외 — 목록/Overview 용."""

    scout_run_id: str
    generated_at: datetime
    code_hash: str
    hypothesis: str | None = None
    candidates_count: int | None = None
    model_used: str | None = None
    prompt_version: str | None = None
    cost_usd: float | None = None
    metadata: dict[str, Any] = {}


class ScoutRunDetail(ScoutRunSummary):
    """단일 조회 — code_text 포함."""

    code_text: str


def _as_dict(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        import json

        return json.loads(v)
    return v


def _row_to_summary(row: Any) -> ScoutRunSummary:
    return ScoutRunSummary(
        scout_run_id=row["scout_run_id"],
        generated_at=row["generated_at"],
        code_hash=row["code_hash"],
        hypothesis=row["hypothesis"],
        candidates_count=row["candidates_count"],
        model_used=row["model_used"],
        prompt_version=row["prompt_version"],
        cost_usd=float(row["cost_usd"]) if row["cost_usd"] is not None else None,
        metadata=_as_dict(row["metadata_json"]) or {},
    )


def _row_to_detail(row: Any) -> ScoutRunDetail:
    return ScoutRunDetail(
        **_row_to_summary(row).model_dump(),
        code_text=row["code_text"],
    )


@router.get("/runs", response_model=list[ScoutRunSummary])
async def list_runs(
    limit: int = 20,
    session: AsyncSession = Depends(get_session),
) -> list[ScoutRunSummary]:
    """최근 scout run limit 개 (code_text 제외)."""
    result = await session.execute(
        text(
            "SELECT scout_run_id, generated_at, code_hash, hypothesis, candidates_count, "
            "model_used, prompt_version, cost_usd, metadata_json "
            "FROM scout_runs ORDER BY generated_at DESC LIMIT :limit"
        ),
        {"limit": max(1, min(limit, 200))},
    )
    return [_row_to_summary(r) for r in result.mappings().all()]


@router.get("/runs/{scout_run_id}", response_model=ScoutRunDetail)
async def get_run(
    scout_run_id: str,
    session: AsyncSession = Depends(get_session),
) -> ScoutRunDetail:
    result = await session.execute(
        text("SELECT * FROM scout_runs WHERE scout_run_id = :id"),
        {"id": scout_run_id},
    )
    row = result.mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"scout run not found: {scout_run_id}")
    return _row_to_detail(row)


@router.get("/dates", response_model=list[str])
async def get_dates(
    limit: int = 30,
    session: AsyncSession = Depends(get_session),
) -> list[str]:
    """scout_runs 가 있는 날짜 목록 (최근순)."""
    result = await session.execute(
        text(
            "SELECT DISTINCT DATE(generated_at AT TIME ZONE 'Asia/Seoul') AS d "
            "FROM scout_runs ORDER BY d DESC LIMIT :limit"
        ),
        {"limit": max(1, min(limit, 365))},
    )
    return [row[0].isoformat() for row in result.all()]


@router.get("/latest", response_model=ScoutRunSummary | dict)
async def get_latest(
    target_date: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> ScoutRunSummary | dict:
    """최신 또는 특정 날짜의 마지막 scout run. 없으면 `{status: "no_data"}`."""
    if target_date:
        day = datetime.fromisoformat(target_date).replace(tzinfo=UTC)
        result = await session.execute(
            text(
                "SELECT scout_run_id, generated_at, code_hash, hypothesis, candidates_count, "
                "model_used, prompt_version, cost_usd, metadata_json FROM scout_runs "
                "WHERE generated_at >= :start AND generated_at < :end "
                "ORDER BY generated_at DESC LIMIT 1"
            ),
            {"start": day, "end": day + timedelta(days=1)},
        )
    else:
        result = await session.execute(
            text(
                "SELECT scout_run_id, generated_at, code_hash, hypothesis, candidates_count, "
                "model_used, prompt_version, cost_usd, metadata_json FROM scout_runs "
                "ORDER BY generated_at DESC LIMIT 1"
            )
        )
    row = result.mappings().one_or_none()
    if row is None:
        return {"status": "no_data"}
    return _row_to_summary(row)
