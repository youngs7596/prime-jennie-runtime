"""LivePositionsPoller happy path — KIS Gateway mock + fakeredis."""

from __future__ import annotations

import json

import fakeredis.aioredis
import httpx
import pytest
import respx
from httpx import Response

from prime_jennie_runtime.monitor.poller import (
    LIVE_POSITIONS_KEY,
    MONITOR_STATUS_KEY,
    LivePositionsPoller,
)


@pytest.mark.asyncio
async def test_tick_once_writes_snapshot():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock.get("http://kis-gateway:8080/balance").mock(
                return_value=Response(
                    200,
                    json={
                        "cash_balance": 500_000,
                        "total_asset": 1_500_000,
                        "stock_eval_amount": 1_000_000,
                        "positions": [
                            {"stock_code": "005930", "quantity": 10},
                            {"stock_code": "035720", "quantity": 5},
                        ],
                    },
                )
            )
            async with httpx.AsyncClient(timeout=5.0) as client:
                poller = LivePositionsPoller(
                    redis_client=redis,
                    gateway_url="http://kis-gateway:8080",
                    client=client,
                )
                count = await poller.tick_once()
                assert count == 2
                assert poller.last_success_ts is not None
                assert poller.last_failure_ts is None

        raw = await redis.get(LIVE_POSITIONS_KEY)
        assert raw is not None
        snap = json.loads(raw)
        assert len(snap["positions"]) == 2
        assert snap["cash_balance"] == 500_000

        raw_status = await redis.get(MONITOR_STATUS_KEY)
        status = json.loads(raw_status)
        assert status["status"] == "online"
        assert status["watching_count"] == 2
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_tick_once_handles_gateway_failure():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock.get("http://kis-gateway:8080/balance").mock(side_effect=httpx.ConnectError("boom"))
            async with httpx.AsyncClient(timeout=5.0) as client:
                poller = LivePositionsPoller(
                    redis_client=redis,
                    gateway_url="http://kis-gateway:8080",
                    client=client,
                )
                count = await poller.tick_once()
                assert count == 0
                assert poller.last_failure_ts is not None
                assert poller.last_success_ts is None

        assert await redis.get(LIVE_POSITIONS_KEY) is None
    finally:
        await redis.aclose()
