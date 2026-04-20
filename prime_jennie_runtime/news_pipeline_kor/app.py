"""News Pipeline long-running runner (entrypoint for `news-pipeline` 컨테이너).

`SchedulerRunner` 가 `scheduled_jobs` (owner='news_pipeline') 를 읽어 apscheduler
트리거로 등록한다. handler_key → async callable 매핑 은 이 파일에 정의.

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

import asyncpg
import httpx
import redis as sync_redis
import redis.asyncio as aioredis
from qdrant_client import QdrantClient

from prime_jennie_runtime.infra.config import AppConfig
from prime_jennie_runtime.infra.db import create_engine
from prime_jennie_runtime.infra.scheduler import PostgresSchedulerStore, SchedulerRunner

from .adapters.exaone_sentiment import LiteLLMSentimentAnalyzer
from .adapters.kure_embedder import KureEmbedder
from .adapters.naver_crawler import NaverNewsCrawler
from .adapters.pg_sentiment_repo import PostgresSentimentRepo
from .adapters.qdrant_store import QdrantVectorStore
from .dedup import RedisDeduplicator
from .pipeline import NewsPipeline

OWNER = "news_pipeline"

logger = logging.getLogger(__name__)


async def _build_pipeline(stack: AsyncExitStack) -> NewsPipeline:
    """외부 의존을 생성해 `NewsPipeline` 을 조립. AsyncExitStack 에 cleanup 등록."""
    vllm_llm_url = os.environ["VLLM_LLM_URL"]
    vllm_llm_model = os.environ["VLLM_LLM_MODEL"]
    vllm_embed_url = os.environ["VLLM_EMBED_URL"]
    vllm_embed_model = os.environ.get("VLLM_EMBED_MODEL", "kure-v1")
    qdrant_url = os.environ["QDRANT_URL"]
    qdrant_collection = os.environ.get("QDRANT_COLLECTION_V3", "v3_news_sentiments")

    http_client = await stack.enter_async_context(httpx.AsyncClient(timeout=10.0))

    # RedisDeduplicator 로 전환 (2026-04-20): 컨테이너 재시작 시 InMemoryDeduplicator
    # 가 리셋돼 crawler window 내 모든 기사를 "신규" 처리 → vLLM 258건 burst →
    # eGPU 팬 굉음. Redis SET+TTL 기반으로 재시작 유지. RedisDeduplicator 는 sync
    # Redis 만 지원하므로 별도 sync 클라이언트를 만든다. 연결 localhost 기준
    # SISMEMBER ~0.1ms, 42건 배치에서 총 ~5ms 블로킹 (event loop 영향 무시 가능).
    _cfg = AppConfig()
    sync_redis_client = sync_redis.Redis.from_url(_cfg.redis.url, decode_responses=True)
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

    cfg = AppConfig()
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

    return NewsPipeline(
        crawler=crawler,
        deduplicator=dedup,
        analyzer=analyzer,
        sentiment_repo=sentiment_repo,
        embedder=embedder,
        vector_store=vector_store,
    )


async def run() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = AppConfig()

    async with AsyncExitStack() as stack:
        pipeline = await _build_pipeline(stack)

        async def crawl_cycle(universe: list[str] | None = None) -> None:
            if not universe:
                logger.warning("crawl_cycle: empty universe, skipping")
                return
            stats = await pipeline.run_cycle(universe)
            logger.info(
                "news cycle: crawled=%d deduped=%d analyzed=%d embedded=%d errors=%d",
                stats.crawled,
                stats.deduped,
                stats.analyzed,
                stats.embedded,
                len(stats.errors),
            )
            if stats.errors:
                for err in stats.errors[:5]:
                    logger.warning("news cycle error: %s", err)

        handlers = {"crawl_cycle": crawl_cycle}

        engine = create_engine(cfg.postgres)
        stack.push_async_callback(engine.dispose)

        redis_client = aioredis.from_url(cfg.redis.url, decode_responses=True)
        stack.push_async_callback(redis_client.aclose)

        from prime_jennie_runtime.infra.heartbeat import HeartbeatPublisher

        heartbeat = HeartbeatPublisher(redis_client, service="news-pipeline")
        await heartbeat.start()
        stack.push_async_callback(heartbeat.stop)

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

        logger.info("news_pipeline runner ready — waiting for signals")
        await stop_event.wait()
        logger.info("news_pipeline runner shutting down")


if __name__ == "__main__":
    asyncio.run(run())
