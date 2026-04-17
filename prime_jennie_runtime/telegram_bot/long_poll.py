"""Telegram getUpdates long-poll 루프.

v2 ``services/telegram/bot.py`` 의 sync polling → async 전환. 단일 task 로 돌면서
``CommandHandler.process_command`` 로 넘기고 응답을 전송.

설계:
- ``httpx.AsyncClient`` 주입 가능 (테스트는 ``respx`` transport).
- 직전 ``update_id + 1`` 을 offset 로 써서 ACK.
- ``stop()`` 으로 정상 종료.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from prime_jennie_runtime.infra.config import TelegramConfig

from .bot import TelegramBot
from .handler import CommandHandler, parse_command

logger = logging.getLogger(__name__)


class LongPollLoop:
    """Telegram getUpdates 기반 명령 수신 루프."""

    def __init__(
        self,
        config: TelegramConfig,
        handler: CommandHandler,
        bot: TelegramBot,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._handler = handler
        self._bot = bot
        self._client = client or httpx.AsyncClient(timeout=float(config.long_poll_timeout_s + 10))
        self._owns_client = client is None
        self._offset: int = 0
        self._running = False

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

        self._running = True
        logger.info(
            "Telegram long-poll started (allowed=%d timeout=%ds)",
            len(self._config.allowed_chat_ids),
            self._config.long_poll_timeout_s,
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
        self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)

        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        text: str = msg.get("text") or ""
        username: str = (msg.get("from") or {}).get("username", "")

        if chat_id is None or not text:
            return

        parsed = parse_command(text)
        if parsed is None:
            return  # 평문 메시지는 무시 (현재 scope)

        cmd, args = parsed
        result = await self._handler.process_command(cmd, args, chat_id, username)
        if result.reply:
            await self._bot.send_message(result.reply, chat_id=str(chat_id))

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


__all__ = ["LongPollLoop"]
