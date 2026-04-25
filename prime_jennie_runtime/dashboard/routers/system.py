"""System API — v3 서비스 헬스 체크.

v2 원본: `prime_jennie/services/dashboard/routers/system.py`

v3 서비스 리스트 (포트는 docker-compose 의 내부 포트를 기준).
환경변수 `DASHBOARD_HEALTH_TARGETS` 로 override 가능 (JSON 또는 `name:url` CSV).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC

import httpx
import redis.asyncio as aioredis
from fastapi import APIRouter
from pydantic import BaseModel

from prime_jennie_runtime.infra.config import AppConfig
from prime_jennie_runtime.infra.heartbeat import DEFAULT_TTL_S, read_heartbeat

router = APIRouter(prefix="/system", tags=["system"])

logger = logging.getLogger(__name__)

# v3 HTTP 엔드포인트 서비스.
_DEFAULT_TARGETS: list[tuple[str, str]] = [
    ("kis-gateway", "http://kis-gateway:8080/health"),
    ("dashboard", "http://dashboard:8090/health"),
    ("monitor", "http://monitor:8091/health"),
    ("control-ui", "http://control-ui:80/"),
    ("telegram-bot", "http://telegram-bot:8000/healthz"),
    # Phase 2.13-6: cloudflared --metrics :2000 활성화 후 /ready 엔드포인트 탐색.
    ("cloudflared", "http://cloudflared:2000/ready"),
]

# v3 pure async daemon — HTTP 없음, heartbeat 우선 관측 + docker container state fallback.
# compose project = prime-jennie-runtime, 컨테이너 = {project}-{service}-1.
# env `DASHBOARD_DAEMONS` (CSV) 로 override. 빈 문자열이면 daemon 체크 건너뜀 (tests 용).
_DEFAULT_DAEMONS = ["slow-loop", "fast-loop", "news-pipeline", "price-scheduler", "job-worker"]
_COMPOSE_PROJECT = "prime-jennie-runtime"


def _load_daemons() -> list[str]:
    raw = os.getenv("DASHBOARD_DAEMONS")
    if raw is None:
        return _DEFAULT_DAEMONS
    raw = raw.strip()
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


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


_OK_LIKE_STATUSES = ("ok", "healthy", "alive", "up")


async def _check(client: httpx.AsyncClient, name: str, url: str) -> ServiceStatus:
    try:
        resp = await client.get(url)
        if resp.status_code == 200:
            data = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            raw_status = data.get("status", "healthy")
            # 서비스마다 응답이 "ok" / "healthy" / "alive" 등으로 제각각이라 UI 카운트가
            # "healthy" 만 세면 정상인 서비스도 unhealthy 로 보인다. 백엔드에서 통일.
            status = "healthy" if str(raw_status).lower() in _OK_LIKE_STATUSES else str(raw_status)
            return ServiceStatus(
                name=name,
                url=url,
                status=status,
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


async def _check_daemon_heartbeat(
    redis_client: aioredis.Redis | None, name: str
) -> ServiceStatus | None:
    """Application heartbeat 가 TTL 내면 healthy. 없으면 None (docker state 로 fallback)."""
    if redis_client is None:
        return None
    try:
        hb = await read_heartbeat(redis_client, name)
    except Exception as e:
        logger.warning("heartbeat read error service=%s err=%s", name, e)
        return None
    if hb is None:
        return None
    age = float(hb["age_seconds"])
    if age > DEFAULT_TTL_S:
        # TTL 지났는데도 키가 남아있는 경우 (예: Redis clock skew) → stale 로 취급
        return None
    # started_at 기반 uptime
    from datetime import datetime

    uptime: float | None = None
    try:
        started_iso = hb.get("started_at")
        if isinstance(started_iso, str) and started_iso:
            started = datetime.fromisoformat(started_iso)
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            uptime = (datetime.now(tz=UTC) - started).total_seconds()
    except (ValueError, TypeError):
        pass
    return ServiceStatus(
        name=name,
        url=f"redis://heartbeat:{name}",
        status="healthy",
        uptime_seconds=uptime,
        message=f"heartbeat {age:.0f}s ago (pid={hb.get('pid')})",
    )


def _check_daemon_container(name: str) -> ServiceStatus:
    """Docker socket fallback — heartbeat 없을 때 프로세스가 떠있긴 한지 확인."""
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
            from datetime import datetime

            try:
                ts = started_at.split(".")[0] + "+00:00" if "+" not in started_at else started_at
                started = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                uptime = (datetime.now(tz=UTC) - started).total_seconds()
            except Exception:
                pass
        if restarting:
            return ServiceStatus(name=name, url=url, status="unhealthy", message="restarting")
        if running:
            # 컨테이너는 running 인데 heartbeat 가 없음 → degraded (loop deadlock 의심)
            return ServiceStatus(
                name=name,
                url=url,
                status="unhealthy",
                uptime_seconds=uptime,
                message="container running but no heartbeat",
            )
        return ServiceStatus(
            name=name,
            url=url,
            status="unreachable",
            message=f"state={state.get('Status', 'unknown')}",
        )
    except Exception as e:
        return ServiceStatus(
            name=name,
            url=url,
            status="unreachable",
            message=f"{type(e).__name__}: {str(e)[:100]}",
        )


async def _check_daemon(redis_client: aioredis.Redis | None, name: str) -> ServiceStatus:
    hb_status = await _check_daemon_heartbeat(redis_client, name)
    if hb_status is not None:
        return hb_status
    return _check_daemon_container(name)


async def _get_redis_client() -> aioredis.Redis | None:
    """dashboard process lifespan 동안 연결 하나 공유 — 첫 호출에 lazy init."""
    global _REDIS_CLIENT
    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT
    try:
        cfg = AppConfig()
        _REDIS_CLIENT = aioredis.from_url(cfg.redis.url, decode_responses=False)
        return _REDIS_CLIENT
    except Exception as e:
        logger.warning("dashboard heartbeat redis init failed: %s", e)
        return None


_REDIS_CLIENT: aioredis.Redis | None = None


@router.get("/health", response_model=list[ServiceStatus])
async def get_all_health() -> list[ServiceStatus]:
    """등록된 모든 서비스의 /health 상태 + daemon heartbeat (+ docker fallback)."""
    results: list[ServiceStatus] = []
    async with httpx.AsyncClient(timeout=3.0) as client:
        for name, url in _load_targets():
            results.append(await _check(client, name, url))
    daemons = _load_daemons()
    if daemons:
        redis_client = await _get_redis_client()
        for daemon_name in daemons:
            results.append(await _check_daemon(redis_client, daemon_name))
    return results
