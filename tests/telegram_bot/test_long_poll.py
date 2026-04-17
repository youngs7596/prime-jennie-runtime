"""LongPollLoop 테스트 — _dispatch / _poll_once 단위 검증.

장시간 구동되는 ``run()`` 루프 자체는 단위 테스트 scope 밖 (smoke 는 통합 수준).
대신 getUpdates 응답 → 명령 분기 → control.commands publish 경로를 단위로 쪼갠다.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from prime_jennie_runtime.infra.config import TelegramConfig
from prime_jennie_runtime.infra.redis_streams import STREAM_CONTROL_COMMANDS
from prime_jennie_runtime.telegram_bot.bot import TelegramBot
from prime_jennie_runtime.telegram_bot.handler import CommandHandler
from prime_jennie_runtime.telegram_bot.long_poll import LongPollLoop


def _config(**overrides) -> TelegramConfig:
    base = {
        "bot_token": "tok",
        "chat_id": "1001",
        "allowed_chat_ids": ["1001"],
        "long_poll_timeout_s": 1,
        "command_min_interval_s": 0,
    }
    base.update(overrides)
    return TelegramConfig(**base)


def _update(update_id: int, text: str, chat_id: int = 1001) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "from": {"id": chat_id, "username": "youngs75"},
            "chat": {"id": chat_id, "type": "private"},
            "date": int(datetime.now(UTC).timestamp()),
            "text": text,
        },
    }


@pytest.mark.asyncio
async def test_run_refuses_empty_allowlist_and_returns(fake_redis):
    cfg = _config(allowed_chat_ids=[])
    handler = CommandHandler(fake_redis, cfg)
    bot = TelegramBot(cfg)
    loop = LongPollLoop(cfg, handler, bot)
    await loop.run()  # 즉시 return
    await loop.close()
    await bot.close()
    assert loop._offset == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_dispatch_pause_command_publishes(fake_redis):
    cfg = _config()
    handler = CommandHandler(fake_redis, cfg)

    async with respx.mock(base_url="https://api.telegram.org") as mock:
        mock.post("/bottok/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        )
        bot = TelegramBot(cfg)
        loop = LongPollLoop(cfg, handler, bot)

        await loop._dispatch(_update(42, "/pause 리스크"))

        await loop.close()
        await bot.close()

    msgs = await fake_redis.xrange(STREAM_CONTROL_COMMANDS)
    assert len(msgs) == 1
    assert b"pause" in msgs[0][1][b"payload"]


@pytest.mark.asyncio
async def test_dispatch_non_slash_text_ignored(fake_redis):
    cfg = _config()
    handler = CommandHandler(fake_redis, cfg)
    bot = TelegramBot(cfg)
    loop = LongPollLoop(cfg, handler, bot)

    await loop._dispatch(_update(1, "hello there"))

    await loop.close()
    await bot.close()

    msgs = await fake_redis.xrange(STREAM_CONTROL_COMMANDS)
    assert msgs == []


@pytest.mark.asyncio
async def test_dispatch_advances_offset_even_on_reject(fake_redis):
    """거부된 chat 의 update 도 update_id 는 ACK 되어야 (재처리 방지)."""
    cfg = _config(allowed_chat_ids=["1001"])
    handler = CommandHandler(fake_redis, cfg)

    async with respx.mock(base_url="https://api.telegram.org") as mock:
        mock.post("/bottok/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
        bot = TelegramBot(cfg)
        loop = LongPollLoop(cfg, handler, bot)

        await loop._dispatch(_update(100, "/stop 확인", chat_id=9999))  # allowlist 밖
        await loop._dispatch(_update(101, "/status"))  # 허용

        await loop.close()
        await bot.close()

    assert loop._offset == 102  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_poll_once_handles_multiple_updates(fake_redis):
    cfg = _config()
    handler = CommandHandler(fake_redis, cfg)

    async with respx.mock(base_url="https://api.telegram.org") as mock:
        mock.post("/bottok/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
        mock.get("/bottok/getUpdates").mock(
            return_value=httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        _update(200, "/pause"),
                        _update(201, "/resume"),
                    ],
                },
            )
        )

        bot = TelegramBot(cfg)
        loop = LongPollLoop(cfg, handler, bot)

        await loop._poll_once()

        await loop.close()
        await bot.close()

    msgs = await fake_redis.xrange(STREAM_CONTROL_COMMANDS)
    assert len(msgs) == 2


@pytest.mark.asyncio
async def test_poll_once_on_api_error_no_crash(fake_redis, monkeypatch):
    cfg = _config()
    handler = CommandHandler(fake_redis, cfg)

    # backoff sleep 을 0 으로 (테스트 2초 블록 방지)
    import prime_jennie_runtime.telegram_bot.long_poll as lp

    async def _fast_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(lp.asyncio, "sleep", _fast_sleep)

    async with respx.mock(base_url="https://api.telegram.org") as mock:
        mock.get("/bottok/getUpdates").mock(return_value=httpx.Response(500, text="boom"))
        bot = TelegramBot(cfg)
        loop = LongPollLoop(cfg, handler, bot)

        await loop._poll_once()  # 로그만 찍고 복귀

        await loop.close()
        await bot.close()

    assert loop._offset == 0  # type: ignore[attr-defined]
