"""Dashboard Backend API — 대시보드용 통합 REST API.

v2 `prime_jennie/services/dashboard/app.py` 포팅 (MariaDB/SQLModel → Postgres/async).

라우터:
- /api/portfolio  — 포트폴리오 / 보유 종목 / 자산 히스토리
- /api/macro      — 매크로 인사이트 (macro_runs)
- /api/scout      — Scout run 이력 (scout_runs)
- /api/watchlist  — 워치리스트 (Redis + position_sheets)
- /api/trades     — 거래 기록 (executions)
- /api/llm        — LLM 사용량 (Redis llm:stats:*)
- /api/system     — v3 서비스 헬스 체크
- /api/airflow    — scheduled_jobs/scheduled_job_runs CRUD (v3 apscheduler 대체)
- /api/logs       — Loki 프록시
- /api/control    — `v3:control.commands` stream publish (Phase 2.10 신규)
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from prime_jennie_runtime.infra.config import AppConfig
from prime_jennie_runtime.infra.db import create_engine, create_session_factory

from .routers import (
    airflow,
    control,
    llm_stats,
    logs,
    macro,
    portfolio,
    scout,
    system,
    trades,
    watchlist,
)

logger = logging.getLogger(__name__)

_DEFAULT_CORS = "http://localhost,http://localhost:80,http://localhost:3000,http://127.0.0.1"


def _cors_origins() -> list[str]:
    raw = os.getenv("DASHBOARD_CORS_ORIGINS", _DEFAULT_CORS)
    return [o.strip() for o in raw.split(",") if o.strip()]


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Dashboard FastAPI 앱 팩토리.

    lifespan 에서 engine/redis 연결을 app.state 에 올리고 shutdown 에 닫는다.
    테스트는 config/engine/redis 를 직접 주입하거나 `app.state` 를 덮어 쓰면 된다.
    """

    cfg = config or AppConfig()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = create_engine(cfg.postgres)
        session_factory = create_session_factory(engine)
        redis_client = aioredis.from_url(cfg.redis.url, decode_responses=True)
        app.state.config = cfg
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.redis = redis_client
        try:
            yield
        finally:
            await redis_client.aclose()
            await engine.dispose()

    app = FastAPI(title="prime-jennie-runtime dashboard", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(portfolio.router, prefix="/api")
    app.include_router(macro.router, prefix="/api")
    app.include_router(scout.router, prefix="/api")
    app.include_router(watchlist.router, prefix="/api")
    app.include_router(trades.router, prefix="/api")
    app.include_router(llm_stats.router, prefix="/api")
    app.include_router(system.router, prefix="/api")
    app.include_router(airflow.router, prefix="/api")
    app.include_router(logs.router, prefix="/api")
    app.include_router(control.router, prefix="/api")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "healthy", "service": "dashboard"}

    return app


app = create_app()
