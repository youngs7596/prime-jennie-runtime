"""Airflow-compat API — v3 apscheduler 기반 `scheduled_jobs` 테이블 CRUD.

v2 원본: `prime_jennie/services/dashboard/routers/airflow.py` (Airflow 3 REST v2 래퍼)

v3 는 Airflow 를 쓰지 않고 apscheduler + `scheduled_jobs` 테이블로 동일 역할을 한다.
frontend 계약(=v2 Airflow response shape)을 최대한 유지하기 위해 prefix `/airflow`
그대로 두고 v2 `list_dags` / `trigger_dag` 과 호환되는 필드를 반환한다.
추가로 v3 전용 scheduled_jobs CRUD 엔드포인트(`GET/POST/PATCH/DELETE /jobs`)도 제공.

단일 진실 공급원: migrations/005 (`scheduled_jobs`, `scheduled_job_runs`).
Redis pub/sub `scheduler.reload:{owner}` 로 runner 에게 reload 트리거.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import JSON, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from prime_jennie_runtime.infra.scheduler import RELOAD_CHANNEL_PREFIX

from ..deps import get_redis, get_session

router = APIRouter(prefix="/airflow", tags=["airflow"])

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- schemas


class ScheduledJob(BaseModel):
    """`scheduled_jobs` 한 행 — frontend CRUD 용 스키마."""

    id: str
    owner: str
    handler_key: str
    cron: str
    kwargs: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    last_duration_ms: int | None = None
    next_run_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ScheduledJobCreate(BaseModel):
    id: str
    owner: str
    handler_key: str
    cron: str
    kwargs: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ScheduledJobPatch(BaseModel):
    handler_key: str | None = None
    cron: str | None = None
    kwargs: dict[str, Any] | None = None
    enabled: bool | None = None


class ScheduledJobRun(BaseModel):
    run_id: int
    job_id: str
    started_at: datetime
    finished_at: datetime | None = None
    status: str
    error: str | None = None
    duration_ms: int | None = None


class DagSummary(BaseModel):
    """v2 Airflow `list_dags` response 와 필드 동형. frontend 계약 유지용."""

    dag_id: str
    description: str | None = None
    schedule_interval: str | None = None
    next_dagrun: datetime | None = None
    last_run_state: str = "unknown"
    last_run_date: datetime | None = None


# ---------------------------------------------------------------------- helpers


def _row_to_job(row: Any) -> ScheduledJob:
    kwargs = row["kwargs"]
    if isinstance(kwargs, str):
        kwargs = json.loads(kwargs or "{}")
    return ScheduledJob(
        id=row["id"],
        owner=row["owner"],
        handler_key=row["handler_key"],
        cron=row["cron"],
        kwargs=dict(kwargs or {}),
        enabled=bool(row["enabled"]),
        last_run_at=row["last_run_at"],
        last_status=row["last_status"],
        last_error=row["last_error"],
        last_duration_ms=row["last_duration_ms"],
        next_run_at=row["next_run_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def _publish_reload(redis_client: aioredis.Redis, owner: str) -> None:
    """Scheduler runner 에게 즉시 reload 신호. 실패해도 polling(30s)으로 반영되므로 silent."""
    try:
        await redis_client.publish(f"{RELOAD_CHANNEL_PREFIX}:{owner}", "reload")
    except Exception:
        logger.warning("scheduler reload publish failed owner=%s", owner, exc_info=True)


# ---------------------------------------------------------------------- v2 호환 (dags)


@router.get("/dags", response_model=list[DagSummary])
async def list_dags(session: AsyncSession = Depends(get_session)) -> list[DagSummary]:
    """활성 `scheduled_jobs` 목록 + 최근 실행 상태. v2 list_dags 응답 모양 유지."""
    result = await session.execute(
        text(
            "SELECT id, handler_key, cron, enabled, last_run_at, last_status, next_run_at "
            "FROM scheduled_jobs WHERE enabled = TRUE ORDER BY id"
        )
    )
    rows = result.mappings().all()
    return [
        DagSummary(
            dag_id=row["id"],
            description=row["handler_key"],
            schedule_interval=row["cron"],
            next_dagrun=row["next_run_at"],
            last_run_state=row["last_status"] or "unknown",
            last_run_date=row["last_run_at"],
        )
        for row in rows
    ]


@router.post("/dags/{dag_id}/trigger")
async def trigger_dag(
    dag_id: str,
    session: AsyncSession = Depends(get_session),
    redis_client: aioredis.Redis = Depends(get_redis),
) -> dict:
    """수동 트리거 요청 — `scheduled_job_runs` 에 status='requested' 행을 남기고 runner 에게 reload 신호.

    apscheduler 는 cron 만 이해하므로 즉시 실행은 runner 측 handler_key 디스패치에 달렸다.
    v3 Phase 2.9 slice2 기준 runner 는 cron 외 수동 실행을 따로 지원하지 않아,
    본 엔드포인트는 요청을 기록하고 향후 `scheduler.trigger:{owner}:{job}` 채널로 확장할 자리.
    """
    result = await session.execute(
        text("SELECT owner FROM scheduled_jobs WHERE id = :id"),
        {"id": dag_id},
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"scheduled job not found: {dag_id}")

    now = datetime.now(UTC)
    await session.execute(
        text(
            "INSERT INTO scheduled_job_runs (job_id, started_at, status) "
            "VALUES (:job_id, :started_at, 'requested')"
        ),
        {"job_id": dag_id, "started_at": now},
    )
    await session.commit()
    await _publish_reload(redis_client, row[0])
    return {"dag_id": dag_id, "status": "requested", "requested_at": now.isoformat()}


# ---------------------------------------------------------------------- v3 전용 CRUD


@router.get("/jobs", response_model=list[ScheduledJob])
async def list_jobs(
    owner: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[ScheduledJob]:
    """scheduled_jobs 전체 목록. owner 필터 선택."""
    if owner:
        result = await session.execute(
            text("SELECT * FROM scheduled_jobs WHERE owner = :owner ORDER BY id"),
            {"owner": owner},
        )
    else:
        result = await session.execute(text("SELECT * FROM scheduled_jobs ORDER BY owner, id"))
    return [_row_to_job(r) for r in result.mappings().all()]


@router.get("/jobs/{job_id}", response_model=ScheduledJob)
async def get_job(job_id: str, session: AsyncSession = Depends(get_session)) -> ScheduledJob:
    result = await session.execute(
        text("SELECT * FROM scheduled_jobs WHERE id = :id"), {"id": job_id}
    )
    row = result.mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"scheduled job not found: {job_id}")
    return _row_to_job(row)


@router.post("/jobs", response_model=ScheduledJob, status_code=201)
async def create_job(
    payload: ScheduledJobCreate,
    session: AsyncSession = Depends(get_session),
    redis_client: aioredis.Redis = Depends(get_redis),
) -> ScheduledJob:
    exists = await session.execute(
        text("SELECT 1 FROM scheduled_jobs WHERE id = :id"), {"id": payload.id}
    )
    if exists.one_or_none() is not None:
        raise HTTPException(status_code=409, detail=f"scheduled job already exists: {payload.id}")

    stmt = text(
        "INSERT INTO scheduled_jobs (id, owner, handler_key, cron, kwargs, enabled) "
        "VALUES (:id, :owner, :handler_key, :cron, :kwargs, :enabled) "
        "RETURNING *"
    ).bindparams(bindparam("kwargs", type_=JSON))
    result = await session.execute(
        stmt,
        {
            "id": payload.id,
            "owner": payload.owner,
            "handler_key": payload.handler_key,
            "cron": payload.cron,
            "kwargs": payload.kwargs,
            "enabled": payload.enabled,
        },
    )
    row = result.mappings().one()
    await session.commit()
    await _publish_reload(redis_client, payload.owner)
    return _row_to_job(row)


@router.patch("/jobs/{job_id}", response_model=ScheduledJob)
async def patch_job(
    job_id: str,
    payload: ScheduledJobPatch,
    session: AsyncSession = Depends(get_session),
    redis_client: aioredis.Redis = Depends(get_redis),
) -> ScheduledJob:
    updates: dict[str, Any] = {}
    if payload.handler_key is not None:
        updates["handler_key"] = payload.handler_key
    if payload.cron is not None:
        updates["cron"] = payload.cron
    if payload.kwargs is not None:
        updates["kwargs"] = payload.kwargs
    if payload.enabled is not None:
        updates["enabled"] = payload.enabled
    if not updates:
        raise HTTPException(status_code=400, detail="no fields to update")

    set_clauses = [f"{key} = :{key}" for key in updates]
    set_clauses.append("updated_at = CURRENT_TIMESTAMP")
    params = {**updates, "id": job_id}

    stmt = text(f"UPDATE scheduled_jobs SET {', '.join(set_clauses)} WHERE id = :id RETURNING *")
    if "kwargs" in updates:
        stmt = stmt.bindparams(bindparam("kwargs", type_=JSON))
    result = await session.execute(stmt, params)
    row = result.mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"scheduled job not found: {job_id}")
    await session.commit()
    await _publish_reload(redis_client, row["owner"])
    return _row_to_job(row)


@router.delete("/jobs/{job_id}", status_code=204)
async def delete_job(
    job_id: str,
    session: AsyncSession = Depends(get_session),
    redis_client: aioredis.Redis = Depends(get_redis),
) -> None:
    result = await session.execute(
        text("DELETE FROM scheduled_jobs WHERE id = :id RETURNING owner"),
        {"id": job_id},
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"scheduled job not found: {job_id}")
    await session.commit()
    await _publish_reload(redis_client, row[0])


@router.get("/jobs/{job_id}/runs", response_model=list[ScheduledJobRun])
async def list_job_runs(
    job_id: str,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
) -> list[ScheduledJobRun]:
    result = await session.execute(
        text(
            "SELECT run_id, job_id, started_at, finished_at, status, error, duration_ms "
            "FROM scheduled_job_runs WHERE job_id = :id ORDER BY started_at DESC LIMIT :limit"
        ),
        {"id": job_id, "limit": max(1, min(limit, 500))},
    )
    return [ScheduledJobRun(**dict(r)) for r in result.mappings().all()]
