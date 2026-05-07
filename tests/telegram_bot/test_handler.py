"""CommandHandler 테스트 — allowlist, rate limit, 7 핸들러, stream publish."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from prime_jennie_runtime.infra.config import TelegramConfig
from prime_jennie_runtime.infra.redis_streams import STREAM_CONTROL_COMMANDS
from prime_jennie_runtime.telegram_bot.control import (
    RESPONSE_DRYRUN_USAGE,
    RESPONSE_LIQUIDATE_USAGE,
    RESPONSE_NOT_ALLOWED,
    RESPONSE_RATE_LIMITED,
    RESPONSE_STOP_CONFIRM,
    STATE_KEY_LIQUIDATE_ARMED,
    STATE_KEY_PAUSE,
    STATE_KEY_STOP,
    ControlCommand,
)
from prime_jennie_runtime.telegram_bot.handler import (
    CommandHandler,
    parse_command,
)

FROZEN_NOW = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)


def _config(**overrides) -> TelegramConfig:
    base = {
        "bot_token": "t",
        "chat_id": "1001",
        "allowed_chat_ids": ["1001", "1002"],
        "command_min_interval_s": 0,  # 기본은 no-limit (테스트별로 오버라이드)
    }
    base.update(overrides)
    return TelegramConfig(**base)


def _handler(redis, **cfg_overrides) -> CommandHandler:
    return CommandHandler(redis, _config(**cfg_overrides), now_fn=lambda: FROZEN_NOW)


async def _last_published(redis) -> ControlCommand | None:
    msgs = await redis.xrange(STREAM_CONTROL_COMMANDS)
    if not msgs:
        return None
    _msg_id, fields = msgs[-1]
    payload = fields.get(b"payload") or fields.get("payload")
    if isinstance(payload, bytes):
        payload = payload.decode()
    return ControlCommand.model_validate_json(payload)


# ---------- parse_command ----------


def test_parse_command_basic():
    assert parse_command("/status") == ("/status", "")
    assert parse_command("/pause 기술적 리스크") == ("/pause", "기술적 리스크")
    assert parse_command("  /resume  ") == ("/resume", "")


def test_parse_command_mention_stripped():
    assert parse_command("/status@primeJennieBot") == ("/status", "")


def test_parse_command_non_slash_returns_none():
    assert parse_command("hello world") is None
    assert parse_command("") is None


# ---------- allowlist fail-safe ----------


@pytest.mark.asyncio
async def test_empty_allowlist_rejects_everyone(fake_redis):
    h = _handler(fake_redis, allowed_chat_ids=[])
    result = await h.process_command("/status", "", chat_id="1001")
    assert result.reply == RESPONSE_NOT_ALLOWED
    assert result.published is None
    # stream 에 아무것도 안 실려야 함
    assert await _last_published(fake_redis) is None


@pytest.mark.asyncio
async def test_non_allowlist_chat_rejected(fake_redis):
    h = _handler(fake_redis)
    result = await h.process_command("/stop", "확인", chat_id="9999")
    assert result.reply == RESPONSE_NOT_ALLOWED


@pytest.mark.asyncio
async def test_allowlist_accepts_int_chat_id(fake_redis):
    h = _handler(fake_redis)
    result = await h.process_command("/status", "", chat_id=1001)  # int
    assert result.reply != RESPONSE_NOT_ALLOWED


# ---------- rate limit ----------


@pytest.mark.asyncio
async def test_rate_limit_blocks_second_call(fake_redis):
    h = _handler(fake_redis, command_min_interval_s=60)
    r1 = await h.process_command("/status", "", chat_id="1001")
    r2 = await h.process_command("/status", "", chat_id="1001")
    assert r1.reply != RESPONSE_RATE_LIMITED
    assert r2.reply == RESPONSE_RATE_LIMITED


@pytest.mark.asyncio
async def test_rate_limit_per_chat(fake_redis):
    """chat_id 별로 별도 카운터."""
    h = _handler(fake_redis, command_min_interval_s=60)
    r1 = await h.process_command("/status", "", chat_id="1001")
    r2 = await h.process_command("/status", "", chat_id="1002")
    assert r1.reply != RESPONSE_RATE_LIMITED
    assert r2.reply != RESPONSE_RATE_LIMITED


# ---------- /help ----------


@pytest.mark.asyncio
async def test_help_does_not_publish(fake_redis):
    h = _handler(fake_redis)
    result = await h.process_command("/help", "", chat_id="1001")
    assert "Prime Jennie" in result.reply
    assert result.published is None


# ---------- /status ----------


@pytest.mark.asyncio
async def test_status_reads_redis_state(fake_redis):
    await fake_redis.set(STATE_KEY_STOP, "1")
    await fake_redis.set(STATE_KEY_PAUSE, "risk_throttle")
    h = _handler(fake_redis)
    result = await h.process_command("/status", "", chat_id="1001")
    assert "긴급정지: ON" in result.reply
    assert "risk_throttle" in result.reply
    assert "DRY_RUN: OFF" in result.reply
    assert result.published is None


# ---------- /pause ----------


@pytest.mark.asyncio
async def test_pause_publishes_with_reason(fake_redis):
    h = _handler(fake_redis)
    result = await h.process_command("/pause", "기술적 리스크", chat_id="1001")
    assert result.published is not None
    assert result.published.kind == "pause"
    assert result.published.reason == "기술적 리스크"
    assert result.published.issued_by == "telegram:1001"
    # stream 에도 기록
    last = await _last_published(fake_redis)
    assert last is not None
    assert last.kind == "pause"


@pytest.mark.asyncio
async def test_pause_default_reason(fake_redis):
    h = _handler(fake_redis)
    result = await h.process_command("/pause", "", chat_id="1001")
    assert result.published is not None
    assert result.published.reason == "manual_pause"


# ---------- /resume ----------


@pytest.mark.asyncio
async def test_resume_publishes_resume(fake_redis):
    h = _handler(fake_redis)
    result = await h.process_command("/resume", "", chat_id="1001")
    assert result.published is not None
    assert result.published.kind == "resume"


# ---------- /stop ----------


@pytest.mark.asyncio
async def test_stop_requires_confirm(fake_redis):
    h = _handler(fake_redis)
    r_bare = await h.process_command("/stop", "", chat_id="1001")
    assert r_bare.reply == RESPONSE_STOP_CONFIRM
    assert r_bare.published is None


@pytest.mark.asyncio
async def test_stop_with_confirm_publishes(fake_redis):
    h = _handler(fake_redis)
    result = await h.process_command("/stop", "확인", chat_id="1001")
    assert result.published is not None
    assert result.published.kind == "emergency_stop"
    assert result.published.reason == "telegram_emergency"


# ---------- /dryrun ----------


@pytest.mark.asyncio
async def test_dryrun_on(fake_redis):
    h = _handler(fake_redis)
    result = await h.process_command("/dryrun", "on", chat_id="1001")
    assert result.published is not None
    assert result.published.kind == "set_dryrun"
    assert result.published.payload == {"enabled": True}


@pytest.mark.asyncio
async def test_dryrun_off(fake_redis):
    h = _handler(fake_redis)
    result = await h.process_command("/dryrun", "off", chat_id="1001")
    assert result.published is not None
    assert result.published.payload == {"enabled": False}


@pytest.mark.asyncio
async def test_dryrun_invalid_arg(fake_redis):
    h = _handler(fake_redis)
    result = await h.process_command("/dryrun", "maybe", chat_id="1001")
    assert result.reply == RESPONSE_DRYRUN_USAGE
    assert result.published is None


# ---------- /liquidate ----------


@pytest.mark.asyncio
async def test_liquidate_arm(fake_redis):
    # arm 은 forced_liquidation:stocks 에 멤버가 있어야 publish (v2 호환)
    await fake_redis.sadd("forced_liquidation:stocks", b"005930")
    h = _handler(fake_redis)
    result = await h.process_command("/liquidate", "arm", chat_id="1001")
    assert result.published is not None
    assert result.published.kind == "liquidate_arm"


@pytest.mark.asyncio
async def test_liquidate_disarm(fake_redis):
    h = _handler(fake_redis)
    result = await h.process_command("/liquidate", "disarm", chat_id="1001")
    assert result.published is not None
    assert result.published.kind == "liquidate_disarm"


@pytest.mark.asyncio
async def test_liquidate_status_is_readonly(fake_redis):
    await fake_redis.set(STATE_KEY_LIQUIDATE_ARMED, "1")
    h = _handler(fake_redis)
    result = await h.process_command("/liquidate", "status", chat_id="1001")
    assert "ON" in result.reply
    assert result.published is None


@pytest.mark.asyncio
async def test_liquidate_invalid_arg(fake_redis):
    h = _handler(fake_redis)
    result = await h.process_command("/liquidate", "nuke", chat_id="1001")
    assert result.reply == RESPONSE_LIQUIDATE_USAGE
    assert result.published is None


# ---------- unknown command ----------


@pytest.mark.asyncio
async def test_unknown_command_no_publish(fake_redis):
    h = _handler(fake_redis)
    result = await h.process_command("/nonexistent", "", chat_id="1001")
    assert "알 수 없는" in result.reply
    assert result.published is None


# ---------- ControlCommand 직렬화 회귀 ----------


@pytest.mark.asyncio
async def test_published_command_is_valid_json(fake_redis):
    h = _handler(fake_redis)
    await h.process_command("/dryrun", "on", chat_id="1001")
    msgs = await fake_redis.xrange(STREAM_CONTROL_COMMANDS)
    assert len(msgs) == 1
    _mid, fields = msgs[0]
    raw = fields[b"payload"].decode()
    obj = json.loads(raw)
    assert obj["kind"] == "set_dryrun"
    assert obj["issued_by"] == "telegram:1001"
