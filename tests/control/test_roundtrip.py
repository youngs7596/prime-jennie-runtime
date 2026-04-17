"""Telegram handler → stream → consumer → state 반영 E2E.

Phase 2.1 + 2.2 의 통합 회귀 1건. 실 서비스와 동일한 경로를 한 프로세스에서 검증.
"""

from __future__ import annotations

import pytest

from prime_jennie_runtime.control.consumer import ControlCommandConsumer
from prime_jennie_runtime.control.state import SystemState
from prime_jennie_runtime.infra.config import TelegramConfig
from prime_jennie_runtime.infra.redis_streams import STREAM_CONTROL_COMMANDS
from prime_jennie_runtime.telegram_bot.control import ControlCommand
from prime_jennie_runtime.telegram_bot.handler import CommandHandler


@pytest.mark.asyncio
async def test_pause_roundtrip_updates_state(fake_redis):
    cfg = TelegramConfig(bot_token="t", allowed_chat_ids=["1001"], command_min_interval_s=0)
    handler = CommandHandler(fake_redis, cfg)
    consumer = ControlCommandConsumer(fake_redis, consumer_name="roundtrip")

    # 1. Telegram 이 /pause 발행
    r = await handler.process_command("/pause", "리스크", chat_id="1001")
    assert r.published is not None

    # 2. stream 에서 꺼내 consumer 에 주입 (직접 apply — run 루프는 통합 scope 외)
    msgs = await fake_redis.xrange(STREAM_CONTROL_COMMANDS)
    assert len(msgs) == 1
    payload = msgs[0][1][b"payload"].decode()
    cmd = ControlCommand.model_validate_json(payload)
    await consumer.apply(cmd)

    # 3. state 반영 확인
    snap = await SystemState(fake_redis).snapshot()
    assert snap.paused
    assert snap.pause_reason == "리스크"

    # 4. 그 상태로 /status 가 올바르게 응답
    r_status = await handler.process_command("/status", "", chat_id="1001")
    assert "리스크" in r_status.reply


@pytest.mark.asyncio
async def test_emergency_then_resume_roundtrip(fake_redis):
    cfg = TelegramConfig(bot_token="t", allowed_chat_ids=["1001"], command_min_interval_s=0)
    handler = CommandHandler(fake_redis, cfg)
    consumer = ControlCommandConsumer(fake_redis, consumer_name="rt2")

    # /stop 확인
    r = await handler.process_command("/stop", "확인", chat_id="1001")
    assert r.published is not None
    msgs = await fake_redis.xrange(STREAM_CONTROL_COMMANDS)
    await consumer.apply(ControlCommand.model_validate_json(msgs[-1][1][b"payload"].decode()))
    assert (await SystemState(fake_redis).snapshot()).stopped

    # /resume
    r = await handler.process_command("/resume", "", chat_id="1001")
    assert r.published is not None
    msgs = await fake_redis.xrange(STREAM_CONTROL_COMMANDS)
    await consumer.apply(ControlCommand.model_validate_json(msgs[-1][1][b"payload"].decode()))
    snap = await SystemState(fake_redis).snapshot()
    assert not snap.stopped
    assert snap.entry_allowed
