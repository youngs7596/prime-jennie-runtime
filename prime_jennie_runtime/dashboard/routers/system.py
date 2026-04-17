"""System API — v3 서비스 헬스 체크.

v2 원본: `prime_jennie/services/dashboard/routers/system.py`

v3 서비스 리스트 (포트는 docker-compose 의 내부 포트를 기준).
환경변수 `DASHBOARD_HEALTH_TARGETS` 로 override 가능 (JSON 또는 `name:url` CSV).
"""

from __future__ import annotations

import json
import logging
import os

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/system", tags=["system"])

logger = logging.getLogger(__name__)

# v3 기본 서비스 목록. docker-compose 네트워크 내부 호스트명:포트.
_DEFAULT_TARGETS: list[tuple[str, str]] = [
    ("kis-gateway", "http://kis-gateway:8080/health"),
    ("slow-loop", "http://slow-loop:8087/health"),
    ("fast-loop", "http://fast-loop:8081/health"),
    ("news-pipeline", "http://news-pipeline:8092/health"),
    ("price-scheduler", "http://price-scheduler:8088/health"),
    ("telegram-bot", "http://telegram-bot:8091/health"),
    ("dashboard", "http://dashboard:8090/health"),
]


def _load_targets() -> list[tuple[str, str]]:
    raw = os.getenv("DASHBOARD_HEALTH_TARGETS")
    if not raw:
        return _DEFAULT_TARGETS
    raw = raw.strip()
    if raw.startswith("["):
        items = json.loads(raw)
        return [(i["name"], i["url"]) for i in items]
    out: list[tuple[str, str]] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        name, _, url = tok.partition(":")
        if url:
            out.append((name.strip(), url.strip()))
    return out


class ServiceStatus(BaseModel):
    name: str
    url: str
    status: str  # "healthy" | "unhealthy" | "unreachable"
    version: str | None = None
    uptime_seconds: float | None = None
    message: str | None = None


async def _check(client: httpx.AsyncClient, name: str, url: str) -> ServiceStatus:
    try:
        resp = await client.get(url)
        if resp.status_code == 200:
            data = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            return ServiceStatus(
                name=name,
                url=url,
                status=data.get("status", "healthy"),
                version=data.get("version"),
                uptime_seconds=data.get("uptime_seconds"),
            )
        return ServiceStatus(
            name=name, url=url, status="unhealthy", message=f"HTTP {resp.status_code}"
        )
    except httpx.ConnectError:
        return ServiceStatus(name=name, url=url, status="unreachable", message="connection refused")
    except Exception as e:
        return ServiceStatus(name=name, url=url, status="unreachable", message=str(e)[:120])


@router.get("/health", response_model=list[ServiceStatus])
async def get_all_health() -> list[ServiceStatus]:
    """등록된 모든 서비스의 /health 상태."""
    results: list[ServiceStatus] = []
    async with httpx.AsyncClient(timeout=3.0) as client:
        for name, url in _load_targets():
            results.append(await _check(client, name, url))
    return results
