"""`v3:notifications` 소비자 — 3종 알림 dispatch 후 텔레그램 전송.

단일 Redis Stream에 서로 다른 모델 타입이 섞이므로 `TypedStreamConsumer`의
단일 model_class 제약을 사용하지 않고, 본 모듈은 자체 소비 루프를 구현한다.
구조는 `TypedStreamConsumer`와 동일 (xgroup_create → xreadgroup → xack).

Handler 흐름:
  1. 스트림에서 엔트리 수신
  2. `XACK` (at-most-once) — 포맷 오류로 반복 실패 방지
  3. payload JSON을 `dispatcher.dispatch`에 넘겨 HTML 문자열 생성
  4. `TelegramBot.send_message(text)` 호출
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

import redis.asyncio as aioredis

from .dispatcher import dispatch

logger = logging.getLogger(__name__)


class _BotLike(Protocol):
    """TelegramBot의 최소 인터페이스 (테스트에서 Fake 주입 목적)."""

    async def send_message(self, text: str, **kwargs: object) -> bool: ...

    async def close(self) -> None: ...


class NotificationConsumer:
    """`v3:notifications` 스트림 소비자."""

    def __init__(
        self,
        client: aioredis.Redis,
        stream: str,
        group: str,
        consumer: str,
        bot: _BotLike,
        batch_size: int = 10,
        block_ms: int = 5000,
    ) -> None:
        self._client = client
        self._stream = stream
        self._group = group
        self._consumer = consumer
        self._bot = bot
        self._batch_size = batch_size
        self._block_ms = block_ms
        self._running = False

    async def ensure_group(self) -> None:
        """Consumer group 생성 (이미 있으면 무시)."""
        try:
            await self._client.xgroup_create(self._stream, self._group, id="0", mkstream=True)
        except aioredis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def run(self) -> None:
        """메시지 소비 루프. `stop()` 또는 CancelledError로 종료."""
        self._running = True
        await self.ensure_group()
        logger.info(
            "Notification consumer started: stream=%s group=%s consumer=%s",
            self._stream,
            self._group,
            self._consumer,
        )

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
                logger.info("Notification consumer cancelled")
                self._running = False
            except aioredis.ConnectionError:
                logger.error("Redis connection lost, retrying in 5s...")
                await asyncio.sleep(5)
            except Exception:
                logger.exception("Notification consumer error")
                await asyncio.sleep(1)

    def stop(self) -> None:
        """소비 루프 중지 플래그."""
        self._running = False

    async def process_one(self) -> int:
        """테스트 편의: 단일 폴링 사이클 실행 후 처리한 메시지 수 반환."""
        await self.ensure_group()
        messages = await self._client.xreadgroup(
            self._group,
            self._consumer,
            {self._stream: ">"},
            count=self._batch_size,
            block=100,
        )
        processed = 0
        if not messages:
            return processed
        for _stream_name, entries in messages:
            for msg_id, data in entries:
                await self._process_message(msg_id, data)
                processed += 1
        return processed

    async def _process_message(self, msg_id: bytes | str, data: dict) -> None:
        """ACK → payload 꺼내기 → dispatch → bot 발송 (at-most-once)."""
        mid = msg_id.decode() if isinstance(msg_id, bytes) else msg_id
        try:
            await self._client.xack(self._stream, self._group, mid)
        except Exception:
            logger.exception("XACK failed: stream=%s id=%s", self._stream, mid)
            return

        payload = data.get(b"payload") or data.get("payload")
        if not payload:
            logger.warning("Empty payload: stream=%s id=%s", self._stream, mid)
            return

        if isinstance(payload, bytes):
            payload = payload.decode()

        text = dispatch(payload)
        if text is None:
            # dispatcher가 이미 로그를 남김 — 메시지는 폐기
            return

        try:
            await self._bot.send_message(text)
        except Exception:
            logger.exception("Telegram send raised: stream=%s id=%s", self._stream, mid)
