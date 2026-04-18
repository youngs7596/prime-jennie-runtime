"""HeartbeatPublisher + read_heartbeat — Phase 2.13-5."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from prime_jennie_runtime.infra.heartbeat import (
    HeartbeatPublisher,
    read_heartbeat,
)


@pytest.mark.asyncio
async def test_publisher_writes_heartbeat_on_start(fake_redis):
    hb = HeartbeatPublisher(fake_redis, service="slow-loop", interval_s=60, ttl_s=90)
    await hb.start()
    try:
        raw = await fake_redis.get("heartbeat:slow-loop")
        assert raw is not None
        data = json.loads(raw)
        assert data["service"] == "slow-loop"
        assert "timestamp" in data and "started_at" in data
    finally:
        await hb.stop()


@pytest.mark.asyncio
async def test_publisher_stop_cancels_loop(fake_redis):
    hb = HeartbeatPublisher(fake_redis, service="fast-loop")
    await hb.start()
    await hb.stop()
    assert hb._task is None


@pytest.mark.asyncio
async def test_publisher_repeats_within_interval(fake_redis):
    hb = HeartbeatPublisher(fake_redis, service="x", interval_s=0, ttl_s=10)
    await hb.start()
    try:
        # interval_s=0 이면 바로 다음 tick 찍음
        await asyncio.sleep(0.05)
        raw = await fake_redis.get("heartbeat:x")
        assert raw is not None
    finally:
        await hb.stop()


@pytest.mark.asyncio
async def test_read_heartbeat_none_when_missing(fake_redis):
    assert await read_heartbeat(fake_redis, "nonexistent") is None


@pytest.mark.asyncio
async def test_read_heartbeat_returns_age_seconds(fake_redis):
    payload = {
        "service": "foo",
        "timestamp": (datetime.now(tz=timezone.utc) - timedelta(seconds=12)).isoformat(),
        "started_at": (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat(),
        "pid": 42,
    }
    await fake_redis.set("heartbeat:foo", json.dumps(payload))
    result = await read_heartbeat(fake_redis, "foo")
    assert result is not None
    assert 10 <= result["age_seconds"] < 20
    assert result["pid"] == 42


@pytest.mark.asyncio
async def test_read_heartbeat_handles_corrupt_json(fake_redis):
    await fake_redis.set("heartbeat:bad", "not-json")
    assert await read_heartbeat(fake_redis, "bad") is None
