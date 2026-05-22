"""Scout API — 결정론 quant scout run 이력 조회.

v3 `scout_runs` 테이블 (migrations/001) 을 대상으로 한다.

2026-05-22 Phase 1 이후 scout 는 LLM codegen 을 폐기하고 v2 결정론 quant 코어로
교체됐다 — 선정 경로 LLM 호출 0회. 따라서 응답 스키마도 결정론 모델을 따른다:
생성 코드(code_text)·모델명(model_used)·LLM 비용(cost_usd) 같은 codegen 개념은
노출하지 않고, 스코어러 버전·팩터 가중치·입력 context 스냅샷을 노출한다.

엔드포인트:
- GET /scout/runs                        — 최근 runs 요약
- GET /scout/runs/{id}                   — 단일 run 상세 (팩터 가중치 + context)
- GET /scout/runs/{id}/candidates        — raw 후보 전수 (screening_candidates, rank 순)
- GET /scout/dates                       — scout_runs 가 있는 날짜 목록
- GET /scout/latest                      — 최신 run 요약 (control-ui Overview 대응)
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
    """결정론 quant scout run 요약 — 목록/Overview 용."""

    scout_run_id: str
    generated_at: datetime
    scorer_version: str | None = None  # 결정론 스코어러 버전 (예: deterministic-quant-v2-port@1)
    summary: str | None = None  # run 요약 문장 (universe→채점→선정)
    candidates_count: int | None = None
    strategy_tags: list[str] = []  # 선정 후보가 사용한 strategy_tag 집합
    runtime_seconds: float | None = None  # 스코어러 실행 소요 (초)


class ScoutRunDetail(ScoutRunSummary):
    """단일 run 상세 — 팩터 가중치 + 입력 context 스냅샷."""

    factor_weights: dict[str, float] = {}  # quant 7팩터 가중치
    context: dict[str, Any] = {}  # context_snapshot_json (universe_size, macro_gate 등)


class ScreeningCandidateRow(BaseModel):
    """migration 012 screening_candidates — scout_run 당 raw 후보 1건.

    promoted_to_sheet_id 와 rejection_reason 중 하나만 채워짐. 둘 다 NULL 이면
    Strategy Engine 가 아직 처리 중인 transient 상태.
    """

    rank: int
    ticker: str
    stock_name: str | None = None
    strategy_tag: str
    conviction: float | None = None
    promoted_to_sheet_id: str | None = None
    rejection_reason: str | None = None
    entry_hint: dict[str, Any] | None = None
    exit_hint: dict[str, Any] | None = None
    factors: dict[str, Any] = {}
    notes: str | None = None


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
    """scout_runs row → 요약. 스코어러 버전은 prompt_version 컬럼, 팩터/태그/소요는
    metadata_json 에서 추출한다.
    """
    meta = _as_dict(row["metadata_json"]) or {}
    return ScoutRunSummary(
        scout_run_id=row["scout_run_id"],
        generated_at=row["generated_at"],
        scorer_version=row["prompt_version"],
        summary=row["hypothesis"],
        candidates_count=row["candidates_count"],
        strategy_tags=meta.get("strategy_tags_used") or [],
        runtime_seconds=meta.get("estimated_runtime_seconds"),
    )


def _row_to_detail(row: Any) -> ScoutRunDetail:
    meta = _as_dict(row["metadata_json"]) or {}
    return ScoutRunDetail(
        **_row_to_summary(row).model_dump(),
        factor_weights=meta.get("factor_weights") or {},
        context=_as_dict(row["context_snapshot_json"]) or {},
    )


_SUMMARY_COLS = (
    "scout_run_id, generated_at, prompt_version, hypothesis, candidates_count, metadata_json"
)


@router.get("/runs", response_model=list[ScoutRunSummary])
async def list_runs(
    limit: int = 20,
    session: AsyncSession = Depends(get_session),
) -> list[ScoutRunSummary]:
    """최근 scout run limit 개."""
    result = await session.execute(
        text(f"SELECT {_SUMMARY_COLS} FROM scout_runs ORDER BY generated_at DESC LIMIT :limit"),
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


@router.get("/runs/{scout_run_id}/candidates", response_model=list[ScreeningCandidateRow])
async def list_candidates(
    scout_run_id: str,
    session: AsyncSession = Depends(get_session),
) -> list[ScreeningCandidateRow]:
    """특정 scout_run 의 raw 후보 전수 (rank 순).

    0건이면 빈 리스트 — 404 를 내지 않는다. "run 은 있지만 결정론 스코어러가
    후보 0 을 선정" 과 "run 자체가 없음" 을 UI 단에서 구분하려면 상위
    `/runs/{id}` 를 먼저 조회해야 한다.
    """
    result = await session.execute(
        text(
            "SELECT sc.rank, sc.ticker, sm.stock_name, sc.strategy_tag, sc.conviction, "
            "sc.promoted_to_sheet_id, sc.rejection_reason, sc.entry_hint_json, "
            "sc.exit_hint_json, sc.factors_json, sc.notes "
            "FROM screening_candidates sc "
            "LEFT JOIN stock_masters sm ON sm.stock_code = sc.ticker "
            "WHERE sc.scout_run_id = :id ORDER BY sc.rank"
        ),
        {"id": scout_run_id},
    )
    return [
        ScreeningCandidateRow(
            rank=row["rank"],
            ticker=row["ticker"],
            stock_name=row["stock_name"],
            strategy_tag=row["strategy_tag"],
            conviction=float(row["conviction"]) if row["conviction"] is not None else None,
            promoted_to_sheet_id=row["promoted_to_sheet_id"],
            rejection_reason=row["rejection_reason"],
            entry_hint=_as_dict(row["entry_hint_json"]),
            exit_hint=_as_dict(row["exit_hint_json"]),
            factors=_as_dict(row["factors_json"]) or {},
            notes=row["notes"],
        )
        for row in result.mappings().all()
    ]


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
                f"SELECT {_SUMMARY_COLS} FROM scout_runs "
                "WHERE generated_at >= :start AND generated_at < :end "
                "ORDER BY generated_at DESC LIMIT 1"
            ),
            {"start": day, "end": day + timedelta(days=1)},
        )
    else:
        result = await session.execute(
            text(f"SELECT {_SUMMARY_COLS} FROM scout_runs ORDER BY generated_at DESC LIMIT 1")
        )
    row = result.mappings().one_or_none()
    if row is None:
        return {"status": "no_data"}
    return _row_to_summary(row)
