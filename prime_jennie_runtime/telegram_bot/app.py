"""Telegram Bot FastAPI 앱 — lifespan으로 consumer 백그라운드 태스크 관리.

v2 `services/telegram/app.py`의 lifespan 패턴 포팅.
threading → asyncio task로 치환 (fast_loop과 동일한 async 모델).

기동 task:
  - `v3:notifications` 소비자 — 항상 기동
  - Telegram getUpdates long-poll — bot_token + allowed_chat_ids 둘 다 있을 때만
  - 헬스체크 엔드포인트

장기능 명령 (/balance /portfolio /price /pnl /buy /sell …) 핸들러를 위해
asyncpg pool + KIS gateway HTTP client 도 lifespan 에서 함께 주입한다.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import asyncpg
import redis.asyncio as aioredis
from fastapi import FastAPI

from prime_jennie_runtime.fast_loop.kis_client import KisClient
from prime_jennie_runtime.infra.config import AppConfig
from prime_jennie_runtime.infra.redis_streams import STREAM_NOTIFICATIONS

from .bot import TelegramBot
from .consumer import NotificationConsumer
from .handler import CommandHandler
from .llm_intent import IntentRouter
from .long_poll import LongPollLoop

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

GROUP_TELEGRAM = "group_telegram"


def _build_redis_client(config: AppConfig) -> aioredis.Redis:
    return aioredis.from_url(config.redis.url, decode_responses=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Consumer 태스크 기동 → yield → 정리."""
    config: AppConfig = app.state.config
    redis_client: aioredis.Redis = app.state.redis

    bot = TelegramBot(config.telegram)
    consumer = NotificationConsumer(
        client=redis_client,
        stream=STREAM_NOTIFICATIONS,
        group=GROUP_TELEGRAM,
        consumer=f"telegram-{config.env}",
        bot=bot,
    )

    task = asyncio.create_task(consumer.run(), name="telegram-notification-consumer")
    app.state.bot = bot
    app.state.consumer = consumer
    app.state.consumer_task = task
    logger.info("Telegram notification consumer task launched")

    pool: asyncpg.Pool | None = None
    kis_client: KisClient | None = None
    intent_router: IntentRouter | None = None
    long_poll: LongPollLoop | None = None
    long_poll_task: asyncio.Task | None = None
    if config.telegram.bot_token and config.telegram.allowed_chat_ids:
        try:
            pool = await asyncpg.create_pool(
                host=config.postgres.host,
                port=config.postgres.port,
                user=config.postgres.user,
                password=config.postgres.password,
                database=config.postgres.db,
                min_size=1,
                max_size=2,
            )
        except Exception:
            logger.exception(
                "telegram-bot PG pool init failed — DB-dependent commands will degrade"
            )
        try:
            kis_client = KisClient(config.kis)
        except Exception:
            logger.exception(
                "telegram-bot KIS client init failed — KIS-dependent commands will degrade"
            )

        intent_router = IntentRouter(redis_client=redis_client)
        long_poll = LongPollLoop(
            config=config.telegram,
            handler=CommandHandler(redis_client, config.telegram, pool=pool, kis_client=kis_client),
            bot=bot,
            intent_router=intent_router,
            redis_client=redis_client,  # offset 영속 — 재시작 시 update 재전달 차단
        )
        long_poll_task = asyncio.create_task(long_poll.run(), name="telegram-long-poll")
        app.state.pool = pool
        app.state.kis_client = kis_client
        app.state.intent_router = intent_router
        app.state.long_poll = long_poll
        app.state.long_poll_task = long_poll_task
        logger.info(
            "Telegram long-poll task launched (allowed_chat_ids=%d pool=%s kis=%s nl=%s)",
            len(config.telegram.allowed_chat_ids),
            "ok" if pool is not None else "missing",
            "ok" if kis_client is not None else "missing",
            "ok" if intent_router.enabled else "off",
        )
    else:
        logger.info(
            "Telegram long-poll disabled (bot_token_set=%s allowed_chat_ids=%d)",
            bool(config.telegram.bot_token),
            len(config.telegram.allowed_chat_ids),
        )

    try:
        yield
    finally:
        consumer.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        if long_poll is not None:
            long_poll.stop()
        if long_poll_task is not None:
            long_poll_task.cancel()
            try:
                await long_poll_task
            except (asyncio.CancelledError, Exception):
                pass
        if long_poll is not None:
            await long_poll.close()
        if intent_router is not None:
            await intent_router.close()
        if kis_client is not None:
            await kis_client.close()
        if pool is not None:
            await pool.close()
        await bot.close()
        logger.info("Telegram bot shut down")


def create_app(config: AppConfig | None = None) -> FastAPI:
    """FastAPI 앱 팩토리. config/redis 주입 가능 (테스트용)."""
    cfg = config or AppConfig()
    app = FastAPI(title="prime-jennie-runtime telegram_bot", lifespan=lifespan)
    app.state.config = cfg
    app.state.redis = _build_redis_client(cfg)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
