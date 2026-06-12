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
from prime_jennie_runtime.telegram_bot.long_poll import OFFSET_KEY, LongPollLoop


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
                        _update(201, "/resume 확인"),
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


# ---------- intent v2 — NL 분류 회신 ----------


class _StubRouter:
    enabled = True

    def __init__(self, result) -> None:
        self._result = result

    async def classify(self, text: str):
        return self._result


@pytest.mark.asyncio
async def test_nl_not_understood_gets_reply(fake_redis):
    """분류 실패가 무응답이 아니라 안내 회신으로 끝난다 (v2)."""
    from prime_jennie_runtime.telegram_bot.llm_intent import IntentResult

    from .fakes import FakeTelegramBot

    cfg = _config()
    handler = CommandHandler(fake_redis, cfg)
    bot = FakeTelegramBot()
    loop = LongPollLoop(cfg, handler, bot, intent_router=_StubRouter(IntentResult(reply="안내문")))

    await loop._dispatch(_update(900, "이해 못할 잡담"))

    await loop.close()
    assert bot.sent_messages == ["안내문"]


@pytest.mark.asyncio
async def test_nl_routed_command_processed(fake_redis):
    from prime_jennie_runtime.telegram_bot.llm_intent import IntentResult

    from .fakes import FakeTelegramBot

    cfg = _config()
    handler = CommandHandler(fake_redis, cfg)
    bot = FakeTelegramBot()
    loop = LongPollLoop(
        cfg, handler, bot, intent_router=_StubRouter(IntentResult(command="/status", args=""))
    )

    await loop._dispatch(_update(901, "지금 상태 어때"))

    await loop.close()
    assert len(bot.sent_messages) == 1
    assert "상태" in bot.sent_messages[0]


# ---------- offset Redis 영속 ----------


@pytest.mark.asyncio
async def test_dispatch_persists_offset_to_redis(fake_redis):
    """update 처리 시작 시점에 offset 이 Redis 에 기록된다 (at-most-once ACK)."""
    cfg = _config()
    handler = CommandHandler(fake_redis, cfg)

    async with respx.mock(base_url="https://api.telegram.org") as mock:
        mock.post("/bottok/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
        bot = TelegramBot(cfg)
        loop = LongPollLoop(cfg, handler, bot, redis_client=fake_redis)

        await loop._dispatch(_update(500, "/status"))

        await loop.close()
        await bot.close()

    raw = await fake_redis.get(OFFSET_KEY)
    assert raw is not None
    assert int(raw) == 501


@pytest.mark.asyncio
async def test_load_offset_survives_restart(fake_redis):
    """재시작(새 인스턴스)이 영속 offset 을 복원 — 과거 update 재실행 차단 (G11)."""
    cfg = _config()
    handler = CommandHandler(fake_redis, cfg)

    async with respx.mock(base_url="https://api.telegram.org") as mock:
        mock.post("/bottok/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
        bot = TelegramBot(cfg)
        first = LongPollLoop(cfg, handler, bot, redis_client=fake_redis)
        await first._dispatch(_update(700, "/status"))
        await first.close()

        second = LongPollLoop(cfg, handler, bot, redis_client=fake_redis)
        await second._load_offset()
        assert second._offset == 701  # type: ignore[attr-defined]

        # 복원된 offset 이전의 재전달 update 는 offset 을 되돌리지 못한다
        await second._dispatch(_update(650, "/status"))
        assert second._offset == 701  # type: ignore[attr-defined]
        assert int(await fake_redis.get(OFFSET_KEY)) == 701

        await second.close()
        await bot.close()


@pytest.mark.asyncio
async def test_load_offset_without_redis_is_noop(fake_redis):
    """redis_client 미주입(기존 테스트 경로) 이면 in-memory 0 출발 그대로."""
    cfg = _config()
    handler = CommandHandler(fake_redis, cfg)
    bot = TelegramBot(cfg)
    loop = LongPollLoop(cfg, handler, bot)
    await loop._load_offset()
    assert loop._offset == 0  # type: ignore[attr-defined]
    await loop.close()
    await bot.close()


@pytest.mark.asyncio
async def test_load_offset_corrupt_value_falls_back_to_zero(fake_redis):
    cfg = _config()
    handler = CommandHandler(fake_redis, cfg)
    await fake_redis.set(OFFSET_KEY, b"not-a-number")
    bot = TelegramBot(cfg)
    loop = LongPollLoop(cfg, handler, bot, redis_client=fake_redis)
    await loop._load_offset()
    assert loop._offset == 0  # type: ignore[attr-defined]
    await loop.close()
    await bot.close()


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
