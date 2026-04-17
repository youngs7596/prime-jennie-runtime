"""TelegramBot 테스트 — respx로 Telegram API mock."""

from __future__ import annotations

import httpx
import pytest
import respx

from prime_jennie_runtime.infra.config import TelegramConfig
from prime_jennie_runtime.telegram_bot.bot import TelegramBot


def _config(**overrides) -> TelegramConfig:
    base = {
        "bot_token": "test-token",
        "chat_id": "123",
        "api_base": "https://api.telegram.org",
        "parse_mode": "HTML",
        "dry_run": False,
    }
    base.update(overrides)
    return TelegramConfig(**base)


@pytest.mark.asyncio
async def test_send_message_success():
    """200 OK + ok=True → True 반환, 요청 payload 확인."""
    async with respx.mock(base_url="https://api.telegram.org") as mock:
        route = mock.post("/bottest-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        )

        bot = TelegramBot(_config())
        ok = await bot.send_message("hello <b>world</b>")
        await bot.close()

        assert ok is True
        assert route.called
        sent = route.calls.last.request
        import json as _json

        body = _json.loads(sent.content.decode())
        assert body["chat_id"] == "123"
        assert body["text"] == "hello <b>world</b>"
        assert body["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_send_message_api_error_no_chat_id():
    """chat_id 미설정 시 에러 로그만 찍고 False 반환 (예외 전파 없음)."""
    bot = TelegramBot(_config(chat_id=""))
    ok = await bot.send_message("hello")
    await bot.close()
    assert ok is False


@pytest.mark.asyncio
async def test_send_message_api_500():
    """API 500 응답 시 False 반환 (예외 전파 없음)."""
    async with respx.mock(base_url="https://api.telegram.org") as mock:
        mock.post("/bottest-token/sendMessage").mock(
            return_value=httpx.Response(500, text="internal error")
        )
        bot = TelegramBot(_config())
        ok = await bot.send_message("hello")
        await bot.close()
        assert ok is False


@pytest.mark.asyncio
async def test_send_message_dry_run():
    """dry_run=True → 실제 HTTP 호출 없음."""
    async with respx.mock(base_url="https://api.telegram.org", assert_all_called=False) as mock:
        route = mock.post("/bottest-token/sendMessage")
        bot = TelegramBot(_config(dry_run=True))
        ok = await bot.send_message("hello")
        await bot.close()
        assert ok is True
        assert not route.called
