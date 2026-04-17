"""Job Worker long-running runner (entrypoint for `job-worker` 컨테이너).

v2 `prime_jennie/services/jobs/app.py` (FastAPI, 2883줄) 대체. FastAPI 엔드포인트
대신 apscheduler 기반 실행으로 전환한 이유:
- Airflow 종속을 제거하기로 결정 (slice2). job-worker 의 역할은 DAG http_conn 을
  받아서 함수를 돌려주는 단순 shell 이었다.
- v3 의 `scheduled_jobs` 테이블이 단일 진실 공급원. owner='job_worker' 의 row 가
  여기서 실행된다.

실행:
    uv run python -m prime_jennie_runtime.jobs.app
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any

import asyncpg
import httpx
import redis.asyncio as aioredis

from prime_jennie_runtime.infra.config import AppConfig
from prime_jennie_runtime.infra.db import create_engine
from prime_jennie_runtime.infra.scheduler import PostgresSchedulerStore, SchedulerRunner

from .council_macro import macro_validate_store
from .maintenance import cleanup_old_data

OWNER = "job_worker"

logger = logging.getLogger(__name__)


def build_handlers(
    *,
    pool: asyncpg.Pool,
    http: httpx.AsyncClient,
    redis_client: aioredis.Redis,
) -> dict[str, Callable[..., Awaitable[Any]]]:
    """handler_key → async callable 매핑.

    새 job 포팅 시 여기에 등록. kwargs 는 scheduled_jobs.kwargs 에서 옴.
    """
    del http  # 현재 핸들러에서 미사용 — 추가 포팅에서 사용 예정.

    async def h_cleanup_old_data(days: int = 365) -> None:
        await cleanup_old_data(pool, days=days)

    async def h_macro_validate_store() -> None:
        await macro_validate_store(redis_client)

    return {
        "cleanup_old_data": h_cleanup_old_data,
        "macro_validate_store": h_macro_validate_store,
    }


async def run() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = AppConfig()

    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(httpx.AsyncClient())

        redis_client = aioredis.from_url(cfg.redis.url, decode_responses=False)
        stack.push_async_callback(redis_client.aclose)

        pool = await asyncpg.create_pool(
            host=cfg.postgres.host,
            port=cfg.postgres.port,
            user=cfg.postgres.user,
            password=cfg.postgres.password,
            database=cfg.postgres.db,
            min_size=1,
            max_size=4,
        )
        stack.push_async_callback(pool.close)

        handlers = build_handlers(pool=pool, http=http, redis_client=redis_client)

        engine = create_engine(cfg.postgres)
        stack.push_async_callback(engine.dispose)

        scheduler = SchedulerRunner(
            owner=OWNER,
            handlers=handlers,
            store=PostgresSchedulerStore(engine),
            redis_client=redis_client,
            timezone_name=cfg.timezone,
        )
        await scheduler.start()
        stack.push_async_callback(scheduler.stop)

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)

        logger.info("job_worker runner ready — waiting for signals")
        await stop_event.wait()
        logger.info("job_worker runner shutting down")


if __name__ == "__main__":
    asyncio.run(run())
