"""Telegram Bot FastAPI 앱 — lifespan으로 consumer 백그라운드 태스크 관리.

v2 `services/telegram/app.py`의 lifespan 패턴 포팅.
threading → asyncio task로 치환 (fast_loop과 동일한 async 모델).

Phase 1 범위:
  - `v3:notifications` 소비자 기동/정리
  - 헬스체크 엔드포인트

Phase 2로 연기:
  - /start, /stop, /poll 폴링 엔드포인트 (양방향 명령)
  - CommandHandler 연동
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

    try:
        yield
    finally:
        consumer.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
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
