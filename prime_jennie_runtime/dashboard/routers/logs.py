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

# promtail 설정의 app 라벨 기준 v3 서비스 목록
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

    query = f'{{app="{service}"}}'
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

    logs: list[dict] = []
    for result in data.get("data", {}).get("result", []):
        for val in result.get("values", []):
            logs.append({"timestamp": val[0], "message": val[1]})
    return {"logs": logs}


@router.get("/services")
async def list_services() -> dict:
    return {"services": _SERVICES}
