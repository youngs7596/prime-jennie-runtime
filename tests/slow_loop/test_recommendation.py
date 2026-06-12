"""추천 발송 — 번호 매핑 + 텔레그램 요약 (시나리오 B §3-2)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import fakeredis.aioredis
import pytest

from prime_jennie_runtime.infra.redis_streams import STREAM_NOTIFICATIONS
from prime_jennie_runtime.slow_loop.recommendation import announce_recommendations
from prime_jennie_runtime.telegram_bot.control import reco_key
from tests.fast_loop.test_consumer_and_tick import _sheet

# _sheet() 의 날짜 (2026-04-16 09:15 KST = 00:15 UTC) 와 같은 날
AS_OF = datetime(2026, 4, 16, 0, 0, 0, tzinfo=UTC)
KEY = reco_key("2026-04-16")


async def _read_alert(redis) -> dict:
    entries = await redis.xrange(STREAM_NOTIFICATIONS)
    assert len(entries) == 1
    payload = entries[0][1][b"payload"]
    return json.loads(payload)


@pytest.mark.asyncio
async def test_announce_stores_mapping_and_sends_summary():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        sheets = [
            _sheet("ps_20260416_005930_a3f2", ticker="005930"),
            _sheet("ps_20260416_000660_b1c2", ticker="000660"),
        ]
        count = await announce_recommendations(redis, None, sheets, as_of_dt=AS_OF)
        assert count == 2

        mapping = await redis.hgetall(KEY)
        assert mapping == {
            b"1": b"ps_20260416_005930_a3f2",
            b"2": b"ps_20260416_000660_b1c2",
        }
        assert await redis.ttl(KEY) > 0

        alert = await _read_alert(redis)
        assert alert["kind"] == "alert"
        assert "추천 2건" in alert["title"]
        assert "/accept" in alert["body"]
        # 종목명 lookup 미주입 (db_engine=None) → ticker fallback
        assert "005930" in alert["body"]
        assert "000660" in alert["body"]
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_announce_numbers_continue_within_day():
    """ad-hoc 재실행 시 기존 번호 뒤에 이어 붙는다 — 번호 재사용 금지."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        await redis.hset(KEY, mapping={"1": "ps_earlier_sheet"})
        sheets = [_sheet("ps_20260416_005930_a3f2", ticker="005930")]
        await announce_recommendations(redis, None, sheets, as_of_dt=AS_OF)
        mapping = await redis.hgetall(KEY)
        assert mapping[b"1"] == b"ps_earlier_sheet"
        assert mapping[b"2"] == b"ps_20260416_005930_a3f2"
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_announce_empty_is_noop():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        assert await announce_recommendations(redis, None, [], as_of_dt=AS_OF) == 0
        assert await redis.exists(KEY) == 0
        assert await redis.xlen(STREAM_NOTIFICATIONS) == 0
    finally:
        await redis.aclose()
