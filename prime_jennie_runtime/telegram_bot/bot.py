"""Telegram Bot API 클라이언트 (async httpx).

v2 `prime_jennie/services/telegram/bot.py`에서 단방향 발신 기능만 추출.
sync httpx → async httpx로 치환. 양방향 명령 수신(get_updates, parse_command 등)은
Phase 2에서 `handler.py`와 함께 포팅.

설계:
  - `dry_run=True`이면 실제 HTTP 호출 없이 로그만 남긴다 (테스트/스테이징 안전).
  - API 오류 시 예외 전파 대신 로그만 찍고 False 반환 (알림 실패가 fast loop를 중단시키지 않도록).
  - chat_id는 config에 1개만 저장. 여러 chat 대상 시 consumer가 루프 처리.
"""

from __future__ import annotations

import logging

import httpx

from prime_jennie_runtime.infra.config import TelegramConfig

logger = logging.getLogger(__name__)

# Telegram 메시지 최대 길이
TELEGRAM_MAX_TEXT_LEN = 4096


class TelegramBot:
    """Telegram Bot API 비동기 클라이언트 (발신 전용)."""

    def __init__(self, config: TelegramConfig, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._base_url = f"{config.api_base.rstrip('/')}/bot{config.bot_token}"
        # 클라이언트 주입 가능 (테스트에서 respx.mock transport 주입)
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None

    async def send_message(
        self,
        text: str,
        chat_id: str | None = None,
        parse_mode: str | None = None,
    ) -> bool:
        """메시지 전송.

        Args:
            text: 메시지 본문 (4096자 초과 시 잘림).
            chat_id: 대상 chat. 생략 시 config.chat_id 사용.
            parse_mode: HTML/MarkdownV2 등. 생략 시 config.parse_mode.

        Returns:
            True 전송 성공, False 실패 (예외 전파 X).
        """
        target_chat = chat_id or self._config.chat_id
        effective_mode = parse_mode or self._config.parse_mode
        truncated = text[:TELEGRAM_MAX_TEXT_LEN]

        if not target_chat:
            logger.error("Telegram chat_id not configured; skipping send")
            return False

        if self._config.dry_run:
            logger.info(
                "Telegram dry_run — would send chat_id=%s len=%d text=%s",
                target_chat,
                len(truncated),
                truncated[:200],
            )
            return True

        if not self._config.bot_token:
            logger.error("Telegram bot_token not configured; skipping send")
            return False

        payload = {
            "chat_id": str(target_chat),
            "text": truncated,
            "parse_mode": effective_mode,
        }

        try:
            resp = await self._client.post(f"{self._base_url}/sendMessage", json=payload)
        except httpx.HTTPError as exc:
            logger.error("Telegram request failed: %s", exc)
            return False

        if resp.status_code != 200:
            logger.error(
                "Telegram API error status=%d body=%s",
                resp.status_code,
                resp.text[:300],
            )
            return False

        try:
            data = resp.json()
        except ValueError:
            logger.error("Telegram non-JSON response: %s", resp.text[:300])
            return False

        if not data.get("ok"):
            logger.error("Telegram API returned ok=False: %s", data)
            return False

        return True

    async def close(self) -> None:
        """내부 httpx 클라이언트 정리 (주입된 경우엔 닫지 않음)."""
        if self._owns_client:
            await self._client.aclose()
