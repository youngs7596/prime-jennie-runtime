"""Telegram getUpdates long-poll 루프.

v2 ``services/telegram/bot.py`` 의 sync polling → async 전환. 단일 task 로 돌면서
``CommandHandler.process_command`` 로 넘기고 응답을 전송.

설계:
- ``httpx.AsyncClient`` 주입 가능 (테스트는 ``respx`` transport).
- 직전 ``update_id + 1`` 을 offset 로 써서 ACK.
- offset 은 Redis 에도 영속 — 재시작 시 텔레그램이 미확인 update 를 재전달해
  과거 명령(특히 /resume)이 맥락 없이 재실행되는 경로 차단 (2026-06-10 전수조사
  G11). Redis 미주입(테스트)이면 in-memory 만 사용.
- ``stop()`` 으로 정상 종료.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import redis.asyncio as aioredis

from prime_jennie_runtime.infra.config import TelegramConfig

from .bot import TelegramBot
from .handler import CommandHandler, parse_command
from .llm_intent import IntentRouter

logger = logging.getLogger(__name__)

# getUpdates offset 영속 키. 값은 "다음에 받을 update_id" (직전 처리분 + 1).
OFFSET_KEY = "telegram:long_poll:offset"


class LongPollLoop:
    """Telegram getUpdates 기반 명령 수신 루프."""

    def __init__(
        self,
        config: TelegramConfig,
        handler: CommandHandler,
        bot: TelegramBot,
        client: httpx.AsyncClient | None = None,
        intent_router: IntentRouter | None = None,
        redis_client: aioredis.Redis | None = None,
    ) -> None:
        self._config = config
        self._handler = handler
        self._bot = bot
        self._client = client or httpx.AsyncClient(timeout=float(config.long_poll_timeout_s + 10))
        self._owns_client = client is None
        self._offset: int = 0
        self._running = False
        self._intent_router = intent_router
        self._redis = redis_client

    def _api_url(self, method: str) -> str:
        return f"{self._config.api_base.rstrip('/')}/bot{self._config.bot_token}/{method}"

    async def run(self) -> None:
        """수신 루프. ``stop()`` 전까지 getUpdates 반복."""
        if not self._config.allowed_chat_ids:
            logger.error("Telegram allowed_chat_ids is empty; long-poll refuses to start")
            return
        if not self._config.bot_token:
            logger.error("Telegram bot_token missing; long-poll refuses to start")
            return

        await self._load_offset()
        self._running = True
        logger.info(
            "Telegram long-poll started (allowed=%d timeout=%ds offset=%d)",
            len(self._config.allowed_chat_ids),
            self._config.long_poll_timeout_s,
            self._offset,
        )
        while self._running:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("long-poll iteration failed")
                await asyncio.sleep(1.0)

    def stop(self) -> None:
        self._running = False

    # ----- offset 영속 -----

    async def _load_offset(self) -> None:
        """Redis 에 영속된 offset 복원. 실패해도 기동은 계속 (in-memory 0 출발).

        0 출발은 텔레그램이 보관 중인 미확인 update 전체 재수신을 뜻하므로,
        복원 실패는 warning 으로 남겨 재실행 위험을 가시화한다.
        """
        if self._redis is None:
            return
        try:
            raw = await self._redis.get(OFFSET_KEY)
        except Exception:
            logger.warning("long-poll offset load failed; starting from 0", exc_info=True)
            return
        if raw is None:
            return
        try:
            value = raw.decode() if isinstance(raw, bytes) else str(raw)
            self._offset = max(self._offset, int(value))
        except (ValueError, UnicodeDecodeError):
            logger.warning("long-poll offset corrupt (%r); starting from 0", raw)

    async def _persist_offset(self) -> None:
        """현재 offset 을 Redis 에 기록. 처리 *시작* 시점에 ACK 하는 at-most-once.

        명령 처리 도중 죽으면 그 명령은 유실되지만 (사용자가 무응답을 보고 재전송),
        반대로 처리 후 기록이면 재시작 때 같은 명령이 한 번 더 실행된다 — 매매
        제어 명령은 유실이 재실행보다 안전하다.
        """
        if self._redis is None:
            return
        try:
            await self._redis.set(OFFSET_KEY, str(self._offset))
        except Exception:
            logger.warning("long-poll offset persist failed (offset=%d)", self._offset)

    async def _poll_once(self) -> None:
        try:
            resp = await self._client.get(
                self._api_url("getUpdates"),
                params={
                    "offset": self._offset,
                    "timeout": self._config.long_poll_timeout_s,
                },
            )
        except httpx.HTTPError as e:
            logger.warning("getUpdates HTTP error: %s", e)
            await asyncio.sleep(2.0)
            return

        if resp.status_code != 200:
            logger.warning("getUpdates status=%d body=%s", resp.status_code, resp.text[:300])
            await asyncio.sleep(2.0)
            return

        data = resp.json()
        if not data.get("ok"):
            logger.warning("getUpdates ok=False: %s", data)
            return

        for update in data.get("result", []) or []:
            await self._dispatch(update)

    async def _dispatch(self, update: dict) -> None:
        advanced = int(update.get("update_id", 0)) + 1
        if advanced > self._offset:
            self._offset = advanced
            await self._persist_offset()

        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        text: str = msg.get("text") or ""
        username: str = (msg.get("from") or {}).get("username", "")

        if chat_id is None or not text:
            return

        parsed = parse_command(text)
        if parsed is None:
            # 평문 메시지 — IntentRouter 가 활성화돼 있으면 LLM 으로 분류 시도
            if self._intent_router is None or not self._intent_router.enabled:
                return
            classified = await self._intent_router.classify(text)
            if classified is None:
                return
            cmd, args = classified
            logger.info("nl_routed text=%r → cmd=%s args=%r", text[:80], cmd, args)
        else:
            cmd, args = parsed

        result = await self._handler.process_command(cmd, args, chat_id, username)
        if result.reply:
            await self._bot.send_message(result.reply, chat_id=str(chat_id))

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


__all__ = ["OFFSET_KEY", "LongPollLoop"]
