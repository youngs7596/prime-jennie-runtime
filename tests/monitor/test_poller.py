"""LivePositionsPoller happy path — KIS Gateway mock + fakeredis."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import fakeredis.aioredis
import httpx
import pytest
import respx
from httpx import Response

from prime_jennie_runtime.kis_gateway.market_hours import MarketCalendar
from prime_jennie_runtime.monitor.poller import (
    HEARTBEAT_KEY,
    LIVE_POSITIONS_KEY,
    MONITOR_STATUS_KEY,
    LivePositionsPoller,
)

_KST = timezone(timedelta(hours=9))


def _calendar_at(now: datetime) -> MarketCalendar:
    """고정 시각을 주입한 MarketCalendar."""
    cal = MarketCalendar()
    cal.set_clock(lambda: now)
    return cal


@pytest.mark.asyncio
async def test_tick_once_writes_snapshot():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock.get("http://kis-gateway:8080/api/balance").mock(
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
            mock.get("http://kis-gateway:8080/api/balance").mock(
                side_effect=httpx.ConnectError("boom")
            )
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


@pytest.mark.asyncio
async def test_current_interval_fast_during_market_hours():
    """정규장 시간엔 interval_sec(촘촘) 적용."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        poller = LivePositionsPoller(
            redis_client=redis,
            gateway_url="http://kis-gateway:8080",
            interval_sec=30.0,
            idle_interval_sec=300.0,
            calendar=_calendar_at(datetime(2026, 5, 22, 11, 0, tzinfo=_KST)),  # 금 11:00
        )
        assert poller._current_interval() == 30.0
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_current_interval_idle_after_close():
    """장 마감 후엔 idle_interval_sec(느슨) 적용."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        poller = LivePositionsPoller(
            redis_client=redis,
            gateway_url="http://kis-gateway:8080",
            interval_sec=30.0,
            idle_interval_sec=300.0,
            calendar=_calendar_at(datetime(2026, 5, 22, 20, 0, tzinfo=_KST)),  # 금 20:00
        )
        assert poller._current_interval() == 300.0
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_current_interval_idle_on_weekend():
    """주말엔 idle_interval_sec 적용 (거래일 아님)."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        poller = LivePositionsPoller(
            redis_client=redis,
            gateway_url="http://kis-gateway:8080",
            interval_sec=30.0,
            idle_interval_sec=300.0,
            calendar=_calendar_at(datetime(2026, 5, 23, 11, 0, tzinfo=_KST)),  # 토 11:00
        )
        assert poller._current_interval() == 300.0
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_heartbeat_ttl_extends_in_idle_window():
    """장 외 느린 주기에도 monitor:heartbeat 가 만료되지 않도록 TTL 이 늘어난다."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock.get("http://kis-gateway:8080/api/balance").mock(
                return_value=Response(200, json={"positions": []})
            )
            async with httpx.AsyncClient(timeout=5.0) as client:
                poller = LivePositionsPoller(
                    redis_client=redis,
                    gateway_url="http://kis-gateway:8080",
                    interval_sec=30.0,
                    idle_interval_sec=300.0,
                    client=client,
                    calendar=_calendar_at(datetime(2026, 5, 23, 11, 0, tzinfo=_KST)),  # 토
                )
                await poller.tick_once()
        ttl = await redis.ttl(HEARTBEAT_KEY)
        assert ttl > 300  # idle 주기(300s)보다 길어야 만료되지 않음
    finally:
        await redis.aclose()
