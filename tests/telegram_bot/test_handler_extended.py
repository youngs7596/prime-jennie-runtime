"""A2/A3/A4/A5 — 확장 명령 핸들러 단위 테스트 (Redis-only / DB / KIS proxy)."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import pytest

from prime_jennie_runtime.infra.config import TelegramConfig
from prime_jennie_runtime.telegram_bot.control import (
    KEY_MAX_BUY_COUNT,
    KEY_MUTE_UNTIL,
    KEY_PRICE_ALERTS,
)
from prime_jennie_runtime.telegram_bot.handler import CommandHandler

FROZEN_NOW = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)


def _config(**overrides) -> TelegramConfig:
    base = {
        "bot_token": "t",
        "chat_id": "1001",
        "allowed_chat_ids": ["1001"],
        "command_min_interval_s": 0,
    }
    base.update(overrides)
    return TelegramConfig(**base)


def _handler(redis, *, pool=None, kis=None) -> CommandHandler:
    return CommandHandler(redis, _config(), now_fn=lambda: FROZEN_NOW, pool=pool, kis_client=kis)


# ----- /mute /unmute -----


@pytest.mark.asyncio
async def test_mute_sets_redis_key(fake_redis):
    h = _handler(fake_redis)
    res = await h.process_command("/mute", "30", chat_id="1001")
    assert "30분" in res.reply
    val = await fake_redis.get(KEY_MUTE_UNTIL)
    assert val is not None
    until = int(val.decode())
    assert until > int(time.time())


@pytest.mark.asyncio
async def test_mute_invalid_input(fake_redis):
    h = _handler(fake_redis)
    assert "사용법" in (await h.process_command("/mute", "abc", chat_id="1001")).reply
    assert "1 이상" in (await h.process_command("/mute", "0", chat_id="1001")).reply


@pytest.mark.asyncio
async def test_unmute_clears_key(fake_redis):
    await fake_redis.set(KEY_MUTE_UNTIL, b"99999")
    h = _handler(fake_redis)
    res = await h.process_command("/unmute", "", chat_id="1001")
    assert "재개" in res.reply
    assert await fake_redis.get(KEY_MUTE_UNTIL) is None


# ----- /alert /alerts -----


class _StubPool:
    def __init__(self, by_code=None, by_name=None):
        self._by_code = by_code or {}
        self._by_name = by_name or {}

    async def fetchrow(self, sql, arg):
        if "stock_code = $1" in sql:
            return self._by_code.get(arg)
        if "stock_name = $1" in sql:
            return self._by_name.get(arg)
        return None


@pytest.mark.asyncio
async def test_alert_with_known_code(fake_redis):
    pool = _StubPool(by_code={"005930": {"stock_code": "005930", "stock_name": "삼성전자"}})
    h = _handler(fake_redis, pool=pool)
    res = await h.process_command("/alert", "005930 70000", chat_id="1001")
    assert "70,000" in res.reply
    raw = await fake_redis.hget(KEY_PRICE_ALERTS, "005930:70000")
    assert raw is not None
    data = json.loads(raw.decode())
    assert data["target_price"] == 70000
    assert data["stock_name"] == "삼성전자"


@pytest.mark.asyncio
async def test_alert_invalid_price(fake_redis):
    pool = _StubPool(by_code={"005930": {"stock_code": "005930", "stock_name": "삼성전자"}})
    h = _handler(fake_redis, pool=pool)
    res = await h.process_command("/alert", "005930 abc", chat_id="1001")
    assert "숫자" in res.reply


@pytest.mark.asyncio
async def test_alert_unknown_name(fake_redis):
    pool = _StubPool()
    h = _handler(fake_redis, pool=pool)
    res = await h.process_command("/alert", "없는 70000", chat_id="1001")
    assert "찾을 수 없" in res.reply


@pytest.mark.asyncio
async def test_alerts_lists_set_alerts(fake_redis):
    await fake_redis.hset(
        KEY_PRICE_ALERTS,
        "005930:70000",
        json.dumps(
            {"stock_code": "005930", "stock_name": "삼성전자", "target_price": 70000}
        ).encode(),
    )
    h = _handler(fake_redis)
    res = await h.process_command("/alerts", "", chat_id="1001")
    assert "삼성전자" in res.reply
    assert "70,000" in res.reply


@pytest.mark.asyncio
async def test_alerts_empty(fake_redis):
    h = _handler(fake_redis)
    res = await h.process_command("/alerts", "", chat_id="1001")
    assert "없습니다" in res.reply


# ----- /maxbuy /config -----


@pytest.mark.asyncio
async def test_maxbuy_sets_redis(fake_redis):
    h = _handler(fake_redis)
    res = await h.process_command("/maxbuy", "5", chat_id="1001")
    assert "5회" in res.reply
    val = await fake_redis.get(KEY_MAX_BUY_COUNT)
    assert val.decode() == "5"


@pytest.mark.asyncio
async def test_maxbuy_out_of_range(fake_redis):
    h = _handler(fake_redis)
    assert "0~20" in (await h.process_command("/maxbuy", "100", chat_id="1001")).reply
    assert "0~20" in (await h.process_command("/maxbuy", "-1", chat_id="1001")).reply


@pytest.mark.asyncio
async def test_config_aggregates_state(fake_redis):
    await fake_redis.set("control.state:dryrun", b"1")
    await fake_redis.set(KEY_MAX_BUY_COUNT, b"7")
    h = _handler(fake_redis)
    res = await h.process_command("/config", "", chat_id="1001")
    assert "DRY_RUN: ON" in res.reply
    assert "7회" in res.reply
