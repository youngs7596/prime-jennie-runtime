"""Notifier — v3:notifications Redis Stream 발행기.

Fast loop의 체결/exit/risk_level_change 이벤트를 publisher(얇은 래퍼)로 발행.
Telegram bot / Control UI가 consumer로 구독.

envelope 없이 각 notification 모델을 그대로 JSON dump. `kind` Literal 필드로
consumer가 타입 분기.
"""

from __future__ import annotations

import logging

import redis.asyncio as aioredis
from pydantic import BaseModel

from prime_jennie_runtime.infra.redis_streams import STREAM_NOTIFICATIONS

logger = logging.getLogger(__name__)


class Notifier:
    """v3:notifications에 알림 발행."""

    def __init__(
        self, client: aioredis.Redis, *, stream: str = STREAM_NOTIFICATIONS, maxlen: int = 10000
    ):
        self._client = client
        self._stream = stream
        self._maxlen = maxlen

    async def emit(self, notification: BaseModel) -> str:
        payload = notification.model_dump_json()
        msg_id = await self._client.xadd(
            self._stream, {"payload": payload}, maxlen=self._maxlen, approximate=True
        )
        if isinstance(msg_id, bytes):
            msg_id = msg_id.decode()
        logger.debug(
            "notification emitted stream=%s id=%s kind=%s",
            self._stream,
            msg_id,
            getattr(notification, "kind", "?"),
        )
        return msg_id
