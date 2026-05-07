"""Telegram Bot FastAPI 앱 — lifespan으로 consumer 백그라운드 태스크 관리.

v2 `services/telegram/app.py`의 lifespan 패턴 포팅.
threading → asyncio task로 치환 (fast_loop과 동일한 async 모델).

기동 task:
  - `v3:notifications` 소비자 — 항상 기동
  - Telegram getUpdates long-poll — bot_token + allowed_chat_ids 둘 다 있을 때만
  - 헬스체크 엔드포인트
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI

from prime_jennie_runtime.infra.config import AppConfig
from prime_jennie_runtime.infra.redis_streams import STREAM_NOTIFICATIONS

from .bot import TelegramBot
from .consumer import NotificationConsumer
from .handler import CommandHandler
from .long_poll import LongPollLoop

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

    long_poll: LongPollLoop | None = None
    long_poll_task: asyncio.Task | None = None
    if config.telegram.bot_token and config.telegram.allowed_chat_ids:
        long_poll = LongPollLoop(
            config=config.telegram,
            handler=CommandHandler(redis_client, config.telegram),
            bot=bot,
        )
        long_poll_task = asyncio.create_task(long_poll.run(), name="telegram-long-poll")
        app.state.long_poll = long_poll
        app.state.long_poll_task = long_poll_task
        logger.info(
            "Telegram long-poll task launched (allowed_chat_ids=%d)",
            len(config.telegram.allowed_chat_ids),
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
