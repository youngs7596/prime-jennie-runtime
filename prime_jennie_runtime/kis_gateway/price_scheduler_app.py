"""Price Scheduler long-running runner (entrypoint for `price-scheduler` 컨테이너).

v2 `collect_minute_chart`/`daily_market_data_collector` DAG 대체.
apscheduler 로 주기 호출 → KIS Gateway REST 엔드포인트를 친 뒤 결과를
`minute_prices` / `daily_prices` 테이블에 upsert.

scheduled_jobs owner='price_scheduler' 에 두 핸들러:
- collect_minute: 5분마다 (*/5 9-15 * * 1-5) — universe 종목 분봉
- collect_daily: 장마감 후 1회 (0 16 * * 1-5) — universe 종목 일봉

실행:
    python -m prime_jennie_runtime.kis_gateway.price_scheduler_app
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import AsyncExitStack

import asyncpg
import httpx
import redis.asyncio as aioredis

from prime_jennie_runtime.infra.config import AppConfig
from prime_jennie_runtime.infra.db import create_engine
from prime_jennie_runtime.infra.scheduler import PostgresSchedulerStore, SchedulerRunner

from .price_repo import PostgresPriceRepo
from .schemas import DailyPrice, MinutePrice

OWNER = "price_scheduler"

logger = logging.getLogger(__name__)


async def _fetch_and_upsert_minute(
    http: httpx.AsyncClient,
    repo: PostgresPriceRepo,
    gateway_url: str,
    ticker: str,
) -> int:
    resp = await http.post(
        f"{gateway_url}/api/market/minute-prices",
        json={"stock_code": ticker},
        timeout=30.0,
    )
    resp.raise_for_status()
    items = [MinutePrice.model_validate(x) for x in resp.json()]
    return await repo.upsert_minute(items)


async def _fetch_and_upsert_daily(
    http: httpx.AsyncClient,
    repo: PostgresPriceRepo,
    gateway_url: str,
    ticker: str,
    days: int,
) -> int:
    resp = await http.post(
        f"{gateway_url}/api/market/daily-prices",
        json={"stock_code": ticker, "days": days},
        timeout=30.0,
    )
    resp.raise_for_status()
    items = [DailyPrice.model_validate(x) for x in resp.json()]
    return await repo.upsert_daily(items)


async def run() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = AppConfig()
    gateway_url = os.environ.get("KIS_GATEWAY_URL", cfg.kis.gateway_url).rstrip("/")

    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(httpx.AsyncClient())
        redis_client = aioredis.from_url(cfg.redis.url, decode_responses=False)
        stack.push_async_callback(redis_client.aclose)

        from prime_jennie_runtime.infra.heartbeat import HeartbeatPublisher

        heartbeat = HeartbeatPublisher(redis_client, service="price-scheduler")
        await heartbeat.start()
        stack.push_async_callback(heartbeat.stop)

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
        repo = PostgresPriceRepo(conn=pool)

        async def collect_minute(universe: list[str] | None = None) -> None:
            if not universe:
                logger.warning("collect_minute: empty universe, skipping")
                return
            ok = 0
            errors: list[str] = []
            for t in universe:
                try:
                    ok += await _fetch_and_upsert_minute(http, repo, gateway_url, t)
                except Exception as e:
                    logger.warning("minute upsert failed ticker=%s: %s", t, e)
                    errors.append(t)
            logger.info(
                "collect_minute: universe=%d upserted=%d errors=%d",
                len(universe),
                ok,
                len(errors),
            )

        async def collect_daily(universe: list[str] | None = None, days: int = 150) -> None:
            if not universe:
                logger.warning("collect_daily: empty universe, skipping")
                return
            ok = 0
            errors: list[str] = []
            for t in universe:
                try:
                    ok += await _fetch_and_upsert_daily(http, repo, gateway_url, t, days)
                except Exception as e:
                    logger.warning("daily upsert failed ticker=%s: %s", t, e)
                    errors.append(t)
            logger.info(
                "collect_daily: universe=%d upserted=%d errors=%d",
                len(universe),
                ok,
                len(errors),
            )

        handlers = {"collect_minute": collect_minute, "collect_daily": collect_daily}

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

        logger.info("price_scheduler runner ready — waiting for signals")
        await stop_event.wait()
        logger.info("price_scheduler runner shutting down")


if __name__ == "__main__":
    asyncio.run(run())
