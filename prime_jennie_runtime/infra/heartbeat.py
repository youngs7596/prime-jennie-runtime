"""Daemon application-level heartbeat — Phase 2.13-5.

Docker container state 는 프로세스가 살아있는지만 알려준다 (scheduler/consumer 가
deadlock 돼도 컨테이너는 "running"). Redis key TTL 기반 heartbeat 를 추가하여
실제 루프가 돌고 있는지 관측 가능하게 만든다.

- `HeartbeatPublisher(service_name)` 를 daemon run() 에서 start/stop
- Dashboard system router 가 `heartbeat:{service}` 를 먼저 본 뒤 docker state 는
  fallback 으로 사용 → heartbeat 가 최근(TTL 내) 이면 "healthy + 최근 ping 시각",
  heartbeat 는 없지만 컨테이너는 running 이면 "degraded"
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import suppress
from datetime import UTC, datetime

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

HEARTBEAT_KEY_PREFIX = "heartbeat:"
DEFAULT_INTERVAL_S = 30
DEFAULT_TTL_S = 90  # 3 × interval 허용 — 일시 지연은 흡수


class HeartbeatPublisher:
    """주기적으로 `heartbeat:{service}` 키를 갱신."""

    def __init__(
        self,
        redis_client: aioredis.Redis,
        service: str,
        *,
        interval_s: int = DEFAULT_INTERVAL_S,
        ttl_s: int = DEFAULT_TTL_S,
    ) -> None:
        self._redis = redis_client
        self._service = service
        self._interval = interval_s
        self._ttl = ttl_s
        self._task: asyncio.Task | None = None
        self._started_at = datetime.now(tz=UTC)
        self._pid = os.getpid()

    @property
    def key(self) -> str:
        return f"{HEARTBEAT_KEY_PREFIX}{self._service}"

    async def start(self) -> None:
        if self._task is not None:
            return
        # 기동 직후 첫 heartbeat 를 동기적으로 한 번 찍고 루프 시작.
        await self._publish_once()
        self._task = asyncio.create_task(self._loop(), name=f"heartbeat-{self._service}")
        logger.info(
            "heartbeat publisher started: service=%s interval=%ds ttl=%ds",
            self._service,
            self._interval,
            self._ttl,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        # 컨테이너 내려가면 heartbeat 키는 TTL 로 자동 소멸.
        logger.info("heartbeat publisher stopped: service=%s", self._service)

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._interval)
                await self._publish_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Redis 일시 오류는 다음 주기에 재시도.
                logger.exception("heartbeat publish failed: service=%s", self._service)

    async def _publish_once(self) -> None:
        payload = {
            "service": self._service,
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "started_at": self._started_at.isoformat(),
            "pid": self._pid,
        }
        await self._redis.set(self.key, json.dumps(payload), ex=self._ttl)


async def read_heartbeat(
    redis_client: aioredis.Redis, service: str
) -> dict[str, str | float] | None:
    """System health 용 reader — heartbeat 가 TTL 내면 dict, 없으면 None.

    Returns dict with: timestamp (iso), started_at (iso), age_seconds (float), pid (int).
    """
    raw = await redis_client.get(f"{HEARTBEAT_KEY_PREFIX}{service}")
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        ts_raw = data.get("timestamp")
        if not isinstance(ts_raw, str):
            return None
        ts = datetime.fromisoformat(ts_raw)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        age = (datetime.now(tz=UTC) - ts).total_seconds()
        return {
            "timestamp": ts_raw,
            "started_at": data.get("started_at", ""),
            "age_seconds": age,
            "pid": data.get("pid", 0),
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
