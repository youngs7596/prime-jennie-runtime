"""Logs API — Loki 로그 조회 프록시.

v2 원본: `prime_jennie/services/dashboard/routers/logs.py`
v3 서비스 라벨 기준으로 `_SERVICES` 만 교체.
"""

from __future__ import annotations

import logging
import os
import time

import httpx
from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/logs", tags=["logs"])

logger = logging.getLogger(__name__)

LOKI_URL = os.getenv("LOKI_URL", "http://loki:3100")

# promtail 이 붙이는 `service` 라벨 (compose `com.docker.compose.service`) 기준 v3 서비스 목록.
# v2 스타일의 `app` 라벨은 compose 에 수동 labels 가 없어 비어 있으므로 사용하지 않는다.
_SERVICES = [
    "kis-gateway",
    "slow-loop",
    "fast-loop",
    "news-pipeline",
    "price-scheduler",
    "telegram-bot",
    "dashboard",
    "monitor",
]


@router.get("/stream")
async def get_logs(
    service: str = Query(..., description="Loki app 라벨 (서비스명)"),
    limit: int = Query(100, description="반환할 로그 라인 수"),
    start: int | None = Query(None, description="시작 타임스탬프 (ns)"),
    end: int | None = Query(None, description="끝 타임스탬프 (ns)"),
) -> dict:
    """Loki `query_range` 프록시."""
    if not start:
        start = int((time.time() - 3600) * 1e9)
    if not end:
        end = int(time.time() * 1e9)

    query = f'{{service="{service}"}}'
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(
                f"{LOKI_URL}/loki/api/v1/query_range",
                params={
                    "query": query,
                    "limit": limit,
                    "start": start,
                    "end": end,
                    "direction": "BACKWARD",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.RequestError as exc:
            logger.warning("Loki connection error: %s", exc)
            raise HTTPException(
                status_code=502, detail="Could not connect to logging service"
            ) from exc
        except Exception as e:
            logger.warning("Error fetching logs: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    # Loki 는 label set 별로 여러 `result` 스트림을 돌려준다 (예: stream=stdout /
    # stream=stderr). 각 스트림은 자체 정렬돼 있지만 그대로 concat 하면 스트림 경계
    # 에서 시각 순서가 섞인다 — BACKWARD 방향 기준 전역 정렬 후 limit 적용.
    logs: list[dict] = []
    for result in data.get("data", {}).get("result", []):
        for val in result.get("values", []):
            logs.append({"timestamp": val[0], "message": val[1]})
    logs.sort(key=lambda x: int(x["timestamp"]), reverse=True)
    if limit and len(logs) > limit:
        logs = logs[:limit]
    return {"logs": logs}


@router.get("/services")
async def list_services() -> dict:
    return {"services": _SERVICES}
