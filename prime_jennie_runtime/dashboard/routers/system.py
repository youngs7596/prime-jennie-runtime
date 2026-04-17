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

# v3 HTTP 엔드포인트 서비스.
_DEFAULT_TARGETS: list[tuple[str, str]] = [
    ("kis-gateway", "http://kis-gateway:8080/health"),
    ("dashboard", "http://dashboard:8090/health"),
    ("monitor", "http://monitor:8091/health"),
    ("control-ui", "http://control-ui:80/"),
    ("telegram-bot", "http://telegram-bot:8000/healthz"),
]

# v3 pure async daemon — HTTP 없음, docker container state 로 관측.
# compose project = prime-jennie-runtime, 컨테이너 = {project}-{service}-1
_DAEMON_CONTAINERS: list[str] = [
    "slow-loop",
    "fast-loop",
    "news-pipeline",
    "price-scheduler",
    "job-worker",
]
_COMPOSE_PROJECT = "prime-jennie-runtime"


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


def _check_daemon(name: str) -> ServiceStatus:
    """Docker socket 으로 daemon 컨테이너 state 확인 (HTTP 없는 서비스용)."""
    container_name = f"{_COMPOSE_PROJECT}-{name}-1"
    url = f"docker://{container_name}"
    try:
        import docker  # type: ignore[import-not-found]

        client = docker.from_env()
        c = client.containers.get(container_name)
        state = c.attrs.get("State", {})
        running = state.get("Running", False)
        restarting = state.get("Restarting", False)
        started_at = state.get("StartedAt")
        uptime = None
        if started_at and running:
            from datetime import datetime, timezone

            try:
                # ISO 8601 with nanoseconds — truncate to microseconds
                ts = started_at.split(".")[0] + "+00:00" if "+" not in started_at else started_at
                started = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                uptime = (datetime.now(timezone.utc) - started).total_seconds()
            except Exception:
                pass
        if restarting:
            return ServiceStatus(name=name, url=url, status="unhealthy", message="restarting")
        if running:
            return ServiceStatus(
                name=name, url=url, status="healthy", uptime_seconds=uptime, message="container running"
            )
        return ServiceStatus(
            name=name, url=url, status="unreachable", message=f"state={state.get('Status', 'unknown')}"
        )
    except Exception as e:
        return ServiceStatus(name=name, url=url, status="unreachable", message=f"{type(e).__name__}: {str(e)[:100]}")


@router.get("/health", response_model=list[ServiceStatus])
async def get_all_health() -> list[ServiceStatus]:
    """등록된 모든 서비스의 /health 상태 + daemon 컨테이너 state."""
    results: list[ServiceStatus] = []
    async with httpx.AsyncClient(timeout=3.0) as client:
        for name, url in _load_targets():
            results.append(await _check(client, name, url))
    for daemon_name in _DAEMON_CONTAINERS:
        results.append(_check_daemon(daemon_name))
    return results
