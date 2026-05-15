"""Coordinator Listener daemon entrypoint.

design: .ai/designs/2026-05-14-agent-coordinator.md §3 Layer C / §11 open question
schema: migrations/017_coordinator_logs.sql
listener: prime_jennie_runtime/coordinator/listener.py

별도 컨테이너로 분리한 이유:
  fast_loop / slow_loop 안에 task 로 띄우면 매매 path crash 가 listener 도 죽이거나
  반대로 listener bug 가 매매 path 를 위협한다 (audit_2026_05_14 의 외톨이 패턴).
  Stage 4 supervisor 가 자연스럽게 들어올 위치이기도 함.

실행:
    python -m prime_jennie_runtime.coordinator.app
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import AsyncExitStack

import asyncpg
import redis.asyncio as aioredis

from prime_jennie_runtime.coordinator.listener import run_listener
from prime_jennie_runtime.fast_loop.notifier import Notifier
from prime_jennie_runtime.infra.config import AppConfig
from prime_jennie_runtime.infra.heartbeat import HeartbeatPublisher

logger = logging.getLogger(__name__)

SERVICE_NAME = "coordinator-listener"


async def run() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = AppConfig()

    async with AsyncExitStack() as stack:
        redis_client = aioredis.from_url(cfg.redis.url, decode_responses=False)
        stack.push_async_callback(redis_client.aclose)

        heartbeat = HeartbeatPublisher(redis_client, service=SERVICE_NAME)
        await heartbeat.start()
        stack.push_async_callback(heartbeat.stop)

        # 소형 pool — listener 는 단일 INSERT 만 (event_log) 수행. 동시성 거의 없음.
        pool = await asyncpg.create_pool(
            host=cfg.postgres.host,
            port=cfg.postgres.port,
            user=cfg.postgres.user,
            password=cfg.postgres.password,
            database=cfg.postgres.db,
            min_size=1,
            max_size=2,
        )
        stack.push_async_callback(pool.close)

        # Stage 2 advisory policies 가 GenericAlertNotification 을 publish 하기 위해
        # Notifier 주입. fast_loop / monitor 와 동일 v3:notifications stream.
        notifier = Notifier(redis_client)
        stack.push_async_callback(notifier.close)

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _request_stop() -> None:
            stop_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _request_stop)

        logger.info("coordinator listener daemon ready — entering run_listener")
        await run_listener(redis_client, pool, stop_event=stop_event, notifier=notifier)
        logger.info("coordinator listener daemon shutting down")


if __name__ == "__main__":
    asyncio.run(run())
