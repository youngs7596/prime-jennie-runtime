"""News Pipeline long-running runner (entrypoint for `news-pipeline` 컨테이너).

2026-04-21 이후 2 async task 로 단순화:
    _collector_loop : crawl (장중 10 분 / 장외 30 분) → XADD v3:news:raw
    _extractor_loop : XREADGROUP v3:news:extractor (BLOCK 2s) → EXAONE metadata →
                      PG news_events → XACK

Archiver/Qdrant/kure-v1 경로 전면 제거 — 메타데이터 RDB 중심.

실행:
    python -m prime_jennie_runtime.news_pipeline_kor.app

환경 (.env 또는 process env):
    POSTGRES_* — pj_runtime 계정
    REDIS_*
    VLLM_LLM_URL / VLLM_LLM_MODEL — EXAONE metadata 추출
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import AsyncExitStack
from datetime import datetime

import asyncpg
import httpx
import redis as sync_redis
import redis.asyncio as aioredis

from prime_jennie_runtime.infra.config import AppConfig

from .adapters.exaone_extractor import LiteLLMEventExtractor
from .adapters.naver_crawler import NaverNewsCrawler
from .adapters.pg_event_repo import PostgresEventRepo
from .dedup import RedisDeduplicator
from .pipeline import NewsPipeline
from .shadow_agent import NewsAgentShadow

logger = logging.getLogger(__name__)

INTERVAL_MARKET_SEC = 10 * 60
INTERVAL_OFF_SEC = 30 * 60

EXTRACTOR_IDLE_SEC = 1
ERROR_BACKOFF_SEC = 30


async def _build_pipeline(
    stack: AsyncExitStack,
) -> tuple[NewsPipeline, sync_redis.Redis, asyncpg.Pool]:
    """외부 의존 생성. ``(pipeline, sync_redis_for_streams, pg_pool)`` 반환."""
    vllm_llm_url = os.environ["VLLM_LLM_URL"]
    vllm_llm_model = os.environ["VLLM_LLM_MODEL"]

    http_client = await stack.enter_async_context(httpx.AsyncClient(timeout=10.0))

    cfg = AppConfig()

    # Redis Streams + Dedup 은 sync redis-py. blocking 호출은 pipeline 내부에서
    # asyncio.to_thread 로 위임되어 event loop 을 막지 않음.
    sync_redis_client = sync_redis.Redis.from_url(cfg.redis.url, decode_responses=False)
    stack.callback(sync_redis_client.close)
    dedup = RedisDeduplicator(redis_client=sync_redis_client)

    crawler = NaverNewsCrawler(client=http_client)
    extractor = LiteLLMEventExtractor(
        model=f"openai/{vllm_llm_model}",
        api_base=vllm_llm_url,
        extra_kwargs={"api_key": "not-needed"},
    )
    # Phase 1 shadow — design `.ai/designs/2026-05-23-news-agent.md` §6.
    # 같은 vLLM endpoint, 별도 prompt. shadow_metadata 로 운영 영향 없이 기록.
    shadow_agent = NewsAgentShadow(
        model=f"openai/{vllm_llm_model}",
        api_base=vllm_llm_url,
        extra_kwargs={"api_key": "not-needed"},
    )

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
    event_repo = PostgresEventRepo(conn=pool)

    pipeline = NewsPipeline(
        crawler=crawler,
        deduplicator=dedup,
        extractor=extractor,
        event_repo=event_repo,
        shadow_agent=shadow_agent,
    )
    return pipeline, sync_redis_client, pool


# ---------------------------------------------------------------------
# Universe / 시간대 헬퍼
# ---------------------------------------------------------------------


_UNIVERSE_SQL = (
    "SELECT stock_code FROM stock_masters "
    "WHERE is_active = TRUE "
    "  AND length(stock_code) = 6 "
    "  AND stock_code ~ '^[0-9]+$' "
    "ORDER BY stock_code"
)


async def _load_universe(pool: asyncpg.Pool) -> list[str]:
    """v2 동일 조건. active + 6자리 숫자 코드."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(_UNIVERSE_SQL)
    return [r["stock_code"] for r in rows]


def _is_market_hours(now: datetime | None = None) -> bool:
    """장중 (07:00~16:00 KST). v2 동일."""
    current = now or datetime.now()
    return 7 <= current.hour < 16


def _collector_interval() -> int:
    return INTERVAL_MARKET_SEC if _is_market_hours() else INTERVAL_OFF_SEC


async def _interruptible_sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


# ---------------------------------------------------------------------
# 2 async loops
# ---------------------------------------------------------------------


async def _collector_loop(
    pipeline: NewsPipeline,
    sync_redis_client: sync_redis.Redis,
    pool: asyncpg.Pool,
    stop: asyncio.Event,
) -> None:
    """주기 크롤 → stream 발행. universe 는 매 cycle DB 에서 재로드."""
    cycle = 0
    logger.info("[collector] loop started")
    while not stop.is_set():
        cycle += 1
        try:
            universe = await _load_universe(pool)
            if not universe:
                logger.warning("[collector cycle %d] empty universe, skipping", cycle)
            else:
                stats = await pipeline.collect_and_publish(universe, sync_redis_client)
                logger.info(
                    "[collector cycle %d] universe=%d crawled=%d published=%d errors=%d",
                    cycle,
                    len(universe),
                    stats.crawled,
                    stats.deduped,
                    len(stats.errors),
                )
                for err in stats.errors[:3]:
                    logger.warning("[collector cycle %d] %s", cycle, err)
        except Exception:
            logger.exception("[collector cycle %d] unexpected error", cycle)
            await _interruptible_sleep(stop, ERROR_BACKOFF_SEC)
            continue

        interval = _collector_interval()
        phase = "market" if _is_market_hours() else "off-hours"
        logger.info("[collector cycle %d] sleep %ds (%s)", cycle, interval, phase)
        await _interruptible_sleep(stop, interval)

    logger.info("[collector] loop stopped")


async def _extractor_loop(
    pipeline: NewsPipeline,
    sync_redis_client: sync_redis.Redis,
    stop: asyncio.Event,
) -> None:
    """Stream BLOCK 상시 대기 → EXAONE metadata 추출 → PG news_events → XACK."""
    logger.info("[extractor] loop started")
    while not stop.is_set():
        try:
            stats = await pipeline.extract_stream_once(sync_redis_client)
            if stats.extracted > 0 or stats.skipped_existing > 0 or stats.errors:
                logger.info(
                    "[extractor] processed=%d skipped_existing=%d errors=%d",
                    stats.extracted,
                    stats.skipped_existing,
                    len(stats.errors),
                )
                for err in stats.errors[:3]:
                    logger.warning("[extractor] %s", err)
            else:
                await _interruptible_sleep(stop, EXTRACTOR_IDLE_SEC)
        except Exception:
            logger.exception("[extractor] unexpected error")
            await _interruptible_sleep(stop, ERROR_BACKOFF_SEC)
    logger.info("[extractor] loop stopped")


# ---------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------


async def run() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = AppConfig()

    async with AsyncExitStack() as stack:
        pipeline, sync_redis_client, pool = await _build_pipeline(stack)

        redis_client = aioredis.from_url(cfg.redis.url, decode_responses=True)
        stack.push_async_callback(redis_client.aclose)

        from prime_jennie_runtime.infra.heartbeat import HeartbeatPublisher

        heartbeat = HeartbeatPublisher(redis_client, service="news-pipeline")
        await heartbeat.start()
        stack.push_async_callback(heartbeat.stop)

        await pipeline.ensure_streams(sync_redis_client)

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)

        logger.info(
            "news_pipeline ready — 2 async loops (collector/extractor). "
            "stream=%s extractor_group=%s",
            "v3:news:raw",
            "v3:news:extractor",
        )

        tasks = [
            asyncio.create_task(
                _collector_loop(pipeline, sync_redis_client, pool, stop_event),
                name="collector",
            ),
            asyncio.create_task(
                _extractor_loop(pipeline, sync_redis_client, stop_event),
                name="extractor",
            ),
        ]

        await stop_event.wait()
        logger.info("news_pipeline shutting down — waiting for loops")
        for t in tasks:
            try:
                await asyncio.wait_for(t, timeout=10)
            except TimeoutError:
                logger.warning("loop %s did not exit within 10s, cancelling", t.get_name())
                t.cancel()


if __name__ == "__main__":
    asyncio.run(run())
