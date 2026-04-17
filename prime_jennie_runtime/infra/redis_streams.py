"""TypedStreamPublisher/Consumer — Redis Streams + Pydantic 직렬화 (async).

v2 prime_jennie.infra.redis.streams 의 async 포팅.

Design:
  - Publisher: Pydantic 모델 → JSON → XADD
  - Consumer: XREADGROUP → JSON → Pydantic 모델 → handler(model)
  - Consumer group: at-most-once delivery (ACK before handler)
  - Pending recovery: 자동으로 미완료 메시지 재처리
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

import redis.asyncio as aioredis
from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# 스트림 이름 상수
STREAM_POSITION_SHEETS = "v3:position_sheets"
STREAM_POSITION_SHEETS_DLQ = "v3:position_sheets.dlq"
# Track C
STREAM_PRICES = "kis:prices"  # KIS Gateway → Fast loop
STREAM_NOTIFICATIONS = "v3:notifications"  # Fast loop → Telegram / UI
STREAM_EXECUTIONS = "v3:executions"  # Fast loop → DB writer / audit


class TypedStreamPublisher(Generic[T]):
    """Pydantic 모델을 Redis Stream에 비동기 발행."""

    def __init__(
        self,
        client: aioredis.Redis,
        stream: str,
        model_class: type[T],
        maxlen: int = 10000,
    ):
        self._client = client
        self._stream = stream
        self._model_class = model_class
        self._maxlen = maxlen

    async def publish(self, message: T) -> str:
        """메시지 발행. 반환값: message ID."""
        data = {"payload": message.model_dump_json()}
        msg_id: bytes = await self._client.xadd(
            self._stream, data, maxlen=self._maxlen, approximate=True
        )
        msg_id_str = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
        logger.debug(
            "Published to %s: id=%s type=%s",
            self._stream,
            msg_id_str,
            self._model_class.__name__,
        )
        return msg_id_str


class TypedStreamConsumer(Generic[T]):
    """Redis Stream 비동기 소비자 — Pydantic 모델 역직렬화 + handler 호출."""

    def __init__(
        self,
        client: aioredis.Redis,
        stream: str,
        group: str,
        consumer: str,
        model_class: type[T],
        handler: Callable[[T], Awaitable[None]],
        batch_size: int = 1,
        block_ms: int = 5000,
    ):
        self._client = client
        self._stream = stream
        self._group = group
        self._consumer = consumer
        self._model_class = model_class
        self._handler = handler
        self._batch_size = batch_size
        self._block_ms = block_ms
        self._running = False

    async def ensure_group(self) -> None:
        """Consumer group 생성 (이미 존재하면 무시)."""
        for attempt in range(30):
            try:
                await self._client.xgroup_create(self._stream, self._group, id="0", mkstream=True)
                return
            except aioredis.ResponseError as e:
                if "BUSYGROUP" in str(e):
                    return
                raise
            except (ConnectionError, OSError):
                logger.warning(
                    "Redis not ready for group '%s' (attempt %d/30)",
                    self._group,
                    attempt + 1,
                )
                await asyncio.sleep(1)
        logger.error("Redis not ready after 30s, group '%s' creation skipped", self._group)

    async def run(self) -> None:
        """메시지 소비 루프. stop()으로 종료."""
        self._running = True
        await self.ensure_group()
        logger.info(
            "Consumer started: stream=%s group=%s consumer=%s",
            self._stream,
            self._group,
            self._consumer,
        )

        await self._recover_pending()

        while self._running:
            try:
                messages = await self._client.xreadgroup(
                    self._group,
                    self._consumer,
                    {self._stream: ">"},
                    count=self._batch_size,
                    block=self._block_ms,
                )
                if not messages:
                    continue

                for _stream_name, entries in messages:
                    for msg_id, data in entries:
                        await self._process_message(msg_id, data)

            except asyncio.CancelledError:
                logger.info("Consumer cancelled")
                self._running = False
            except aioredis.ConnectionError:
                logger.error("Redis connection lost, retrying in 5s...")
                await asyncio.sleep(5)
            except Exception:
                logger.exception("Consumer error")
                await asyncio.sleep(1)

    def stop(self) -> None:
        """소비 루프 중지."""
        self._running = False

    async def _process_message(self, msg_id: bytes | str, data: dict) -> None:
        """ACK → 역직렬화 → handler (at-most-once)."""
        mid = msg_id.decode() if isinstance(msg_id, bytes) else msg_id
        await self._client.xack(self._stream, self._group, mid)

        payload = data.get(b"payload") or data.get("payload")
        if not payload:
            logger.warning("Empty payload: stream=%s id=%s", self._stream, mid)
            return

        if isinstance(payload, bytes):
            payload = payload.decode()

        try:
            model = self._model_class.model_validate_json(payload)
            await self._handler(model)
        except Exception:
            logger.exception("Handler failed: stream=%s id=%s", self._stream, mid)

    async def _recover_pending(self) -> None:
        """미완료 pending 메시지 복구."""
        try:
            pending = await self._client.xpending_range(
                self._stream, self._group, "-", "+", count=100
            )
            if not pending:
                return

            logger.info(
                "Recovering %d pending messages from %s",
                len(pending),
                self._stream,
            )

            msg_ids = [p["message_id"] for p in pending]
            claimed = await self._client.xclaim(
                self._stream,
                self._group,
                self._consumer,
                min_idle_time=60000,
                message_ids=msg_ids,
            )
            for msg_id, data in claimed:
                await self._process_message(msg_id, data)

        except Exception:
            logger.exception("Pending recovery failed")
