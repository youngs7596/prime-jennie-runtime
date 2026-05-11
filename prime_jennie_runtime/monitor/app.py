"""v3 Monitor FastAPI app — `/health`, `/status`, `/metrics`.

v2 `prime_jennie/services/monitor/app.py` 대체 — v3 는 snapshot/metrics 만 담당.
lifespan 에서 `LivePositionsPoller` 를 띄우고 Redis 에 주기적으로 스냅샷을 쓴다.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Response

from prime_jennie_runtime.fast_loop.notifier import Notifier
from prime_jennie_runtime.infra.config import AppConfig

from .metrics import render_metrics
from .poller import LivePositionsPoller

logger = logging.getLogger(__name__)


def _gateway_url(cfg: AppConfig) -> str:
    return os.getenv("KIS_GATEWAY_URL", cfg.kis.gateway_url)


def _poll_interval() -> float:
    try:
        return float(os.getenv("MONITOR_POLL_INTERVAL_SEC", "30"))
    except ValueError:
        return 30.0


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Monitor FastAPI 앱 팩토리."""

    cfg = config or AppConfig()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        redis_client = aioredis.from_url(cfg.redis.url, decode_responses=True)
        # Notifier 는 fast_loop 와 동일한 Redis stream (v3:notifications) 에 알림
        # 발행 → telegram_bot 가 동일 경로로 소비. Slack 미러는 env 가 있을 때만.
        notifier = Notifier(redis_client)
        poller = LivePositionsPoller(
            redis_client=redis_client,
            gateway_url=_gateway_url(cfg),
            interval_sec=_poll_interval(),
            notifier=notifier,
        )
        await poller.__aenter__()
        poller.start()
        app.state.config = cfg
        app.state.redis = redis_client
        app.state.poller = poller
        app.state.notifier = notifier
        try:
            yield
        finally:
            await poller.stop()
            await poller.close()
            await notifier.close()
            await redis_client.aclose()

    app = FastAPI(title="prime-jennie-runtime monitor", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "healthy", "service": "monitor"}

    @app.get("/status")
    async def status() -> dict:
        poller: LivePositionsPoller | None = getattr(app.state, "poller", None)
        if poller is None:
            return {"status": "not_ready"}
        return {
            "status": "online",
            "last_success_ts": poller.last_success_ts,
            "last_failure_ts": poller.last_failure_ts,
            "last_positions_count": poller.last_positions_count,
            "interval_sec": poller._interval,
        }

    @app.get("/metrics")
    async def metrics() -> Response:
        poller: LivePositionsPoller | None = getattr(app.state, "poller", None)
        body = render_metrics(poller)
        return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")

    return app


app = create_app()
