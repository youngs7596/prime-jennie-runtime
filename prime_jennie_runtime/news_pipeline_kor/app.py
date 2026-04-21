"""News Pipeline long-running runner (entrypoint for `news-pipeline` 컨테이너).

v2 ``prime_jennie/services/news/app.py`` 3-thread 상시 구동 모델을 asyncio task 로
이식. ``SchedulerRunner`` 는 제거 — 이 서비스는 cron 으로 구동되지 않는다.

흐름 (3 async task 병렬):
    _collector_loop : 장중 10 분 / 장외 30 분 주기로 crawl → stream 발행
    _analyzer_loop  : stream BLOCK 상시 대기 → LLM → PG upsert → XACK
    _archiver_loop  : stream BLOCK 상시 대기 → embed → Qdrant upsert → XACK

실행:
    python -m prime_jennie_runtime.news_pipeline_kor.app

환경 (.env 또는 process env):
    POSTGRES_* — pj_runtime 계정
    REDIS_*
    VLLM_LLM_URL / VLLM_LLM_MODEL — EXAONE 감성
    VLLM_EMBED_URL / VLLM_EMBED_MODEL — kure-v1 임베딩
    QDRANT_URL — v3_news_sentiments 컬렉션
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
from qdrant_client import QdrantClient

from prime_jennie_runtime.infra.config import AppConfig

from .adapters.exaone_sentiment import LiteLLMSentimentAnalyzer
from .adapters.kure_embedder import KureEmbedder
from .adapters.naver_crawler import NaverNewsCrawler
from .adapters.pg_sentiment_repo import PostgresSentimentRepo
from .adapters.qdrant_store import QdrantVectorStore
from .dedup import RedisDeduplicator
from .pipeline import NewsPipeline

logger = logging.getLogger(__name__)

# Collector 주기 (v2 동일). "상시 구동" 의 상시는 analyzer/archiver 가 stream 을
# BLOCK 으로 상시 소비하는 쪽. 네트워크 크롤은 여전히 주기적이지만 stream 에 얇게
# 퍼지므로 GPU peak 이 평탄해진다.
INTERVAL_MARKET_SEC = 10 * 60
INTERVAL_OFF_SEC = 30 * 60

# analyzer/archiver 가 XREADGROUP BLOCK 2 초 대기 후 idle 일 때 추가로 쉬는 시간.
# BLOCK 자체가 대부분의 idle 대기를 흡수하므로 이 값은 짧게.
ANALYZER_IDLE_SEC = 1
ARCHIVER_IDLE_SEC = 2
ERROR_BACKOFF_SEC = 30


async def _build_pipeline(
    stack: AsyncExitStack,
) -> tuple[NewsPipeline, sync_redis.Redis, asyncpg.Pool]:
    """외부 의존 생성. ``(pipeline, sync_redis_for_streams, pg_pool)`` 반환."""
    vllm_llm_url = os.environ["VLLM_LLM_URL"]
    vllm_llm_model = os.environ["VLLM_LLM_MODEL"]
    vllm_embed_url = os.environ["VLLM_EMBED_URL"]
    vllm_embed_model = os.environ.get("VLLM_EMBED_MODEL", "kure-v1")
    qdrant_url = os.environ["QDRANT_URL"]
    qdrant_collection = os.environ.get("QDRANT_COLLECTION_V3", "v3_news_sentiments")

    http_client = await stack.enter_async_context(httpx.AsyncClient(timeout=10.0))

    cfg = AppConfig()

    # Redis Streams + Dedup 은 sync redis-py. v2 의 XREADGROUP 패턴과 일치하며
    # redis-py sync 가 `block` ms 동안만 blocking. asyncio loop 에선 dedicated
    # task 에서 호출하므로 event loop 영향은 제한적 (loop 간 bounce 를 위해
    # asyncio.to_thread 로 래핑).
    sync_redis_client = sync_redis.Redis.from_url(cfg.redis.url, decode_responses=False)
    stack.callback(sync_redis_client.close)
    dedup = RedisDeduplicator(redis_client=sync_redis_client)

    crawler = NaverNewsCrawler(client=http_client)
    analyzer = LiteLLMSentimentAnalyzer(
        model=f"openai/{vllm_llm_model}",
        api_base=vllm_llm_url,
        extra_kwargs={"api_key": "not-needed"},
    )
    embedder = KureEmbedder(
        api_base=vllm_embed_url,
        model=vllm_embed_model,
        client=http_client,
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
    sentiment_repo = PostgresSentimentRepo(conn=pool)

    qdrant_client = QdrantClient(url=qdrant_url)
    stack.callback(qdrant_client.close)
    vector_store = QdrantVectorStore(
        client=qdrant_client,
        collection_name=qdrant_collection,
        dimension=1024,
    )

    pipeline = NewsPipeline(
        crawler=crawler,
        deduplicator=dedup,
        analyzer=analyzer,
        sentiment_repo=sentiment_repo,
        embedder=embedder,
        vector_store=vector_store,
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
    """v2 동일 조건. active + 6자리 숫자 코드 (우선주 K/L/G suffix 자동 제외)."""
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
    """stop event 가 set 되면 즉시 깨어나는 sleep."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


# ---------------------------------------------------------------------
# 3 async loops
# ---------------------------------------------------------------------


async def _collector_loop(
    pipeline: NewsPipeline,
    sync_redis_client: sync_redis.Redis,
    pool: asyncpg.Pool,
    stop: asyncio.Event,
) -> None:
    """주기 크롤 → stream 발행. universe 는 매 cycle DB 에서 재로드 (v2 동일)."""
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


async def _analyzer_loop(
    pipeline: NewsPipeline,
    sync_redis_client: sync_redis.Redis,
    stop: asyncio.Event,
) -> None:
    """Stream BLOCK 상시 대기 → 배치 LLM → PG upsert → XACK.

    main event loop 에서 직접 await — httpx.AsyncClient 가 이 loop 에 묶여있으므로
    별도 loop 생성 금지. sync redis 호출은 pipeline 내부에서 ``asyncio.to_thread`` 로
    위임돼 event loop 이 BLOCK 동안 멈추지 않는다.
    """
    logger.info("[analyzer] loop started")
    while not stop.is_set():
        try:
            stats = await pipeline.analyze_stream_once(sync_redis_client)
            if stats.analyzed > 0 or stats.errors:
                logger.info(
                    "[analyzer] processed=%d errors=%d",
                    stats.analyzed,
                    len(stats.errors),
                )
                for err in stats.errors[:3]:
                    logger.warning("[analyzer] %s", err)
            else:
                await _interruptible_sleep(stop, ANALYZER_IDLE_SEC)
        except Exception:
            logger.exception("[analyzer] unexpected error")
            await _interruptible_sleep(stop, ERROR_BACKOFF_SEC)
    logger.info("[analyzer] loop stopped")


async def _archiver_loop(
    pipeline: NewsPipeline,
    sync_redis_client: sync_redis.Redis,
    stop: asyncio.Event,
) -> None:
    """Stream BLOCK 상시 대기 → 배치 embed → Qdrant upsert → XACK."""
    logger.info("[archiver] loop started")
    while not stop.is_set():
        try:
            stats = await pipeline.archive_stream_once(sync_redis_client)
            if stats.embedded > 0 or stats.errors:
                logger.info(
                    "[archiver] processed=%d errors=%d",
                    stats.embedded,
                    len(stats.errors),
                )
                for err in stats.errors[:3]:
                    logger.warning("[archiver] %s", err)
            else:
                await _interruptible_sleep(stop, ARCHIVER_IDLE_SEC)
        except Exception:
            logger.exception("[archiver] unexpected error")
            await _interruptible_sleep(stop, ERROR_BACKOFF_SEC)
    logger.info("[archiver] loop stopped")


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

        # heartbeat 는 async redis 로. daemon 가시성은 container state 보다 신뢰.
        redis_client = aioredis.from_url(cfg.redis.url, decode_responses=True)
        stack.push_async_callback(redis_client.aclose)

        from prime_jennie_runtime.infra.heartbeat import HeartbeatPublisher

        heartbeat = HeartbeatPublisher(redis_client, service="news-pipeline")
        await heartbeat.start()
        stack.push_async_callback(heartbeat.stop)

        # Consumer group 을 한번 보장. BUSYGROUP 은 내부에서 무시.
        await pipeline.ensure_streams(sync_redis_client)

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)

        logger.info(
            "news_pipeline ready — 3 async loops (collector/analyzer/archiver). "
            "stream=%s analyzer_group=%s archiver_group=%s",
            "v3:news:raw",
            "v3:news:analyzer",
            "v3:news:archiver",
        )

        tasks = [
            asyncio.create_task(
                _collector_loop(pipeline, sync_redis_client, pool, stop_event),
                name="collector",
            ),
            asyncio.create_task(
                _analyzer_loop(pipeline, sync_redis_client, stop_event), name="analyzer"
            ),
            asyncio.create_task(
                _archiver_loop(pipeline, sync_redis_client, stop_event), name="archiver"
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
