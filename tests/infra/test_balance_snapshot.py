"""infra/balance_snapshot — 잔고 단일 발행/다중 구독 reader."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from prime_jennie_runtime.infra.balance_snapshot import (
    LIVE_POSITIONS_KEY,
    get_balance_snapshot_or_fetch,
    read_balance_snapshot,
)

GATEWAY = "http://kis-gateway:8080"


class _FakeRedis:
    def __init__(self, value: str | None = None, *, raise_on_get: bool = False) -> None:
        self.value = value
        self.raise_on_get = raise_on_get

    async def get(self, key: str):
        if self.raise_on_get:
            raise ConnectionError("redis down")
        assert key == LIVE_POSITIONS_KEY
        return self.value


def _snapshot_json(*, age_sec: float = 0.0, cash: int = 1_500_000) -> str:
    updated = datetime.now(UTC) - timedelta(seconds=age_sec)
    return json.dumps(
        {
            "positions": [{"stock_code": "005930", "quantity": 10}],
            "updated_at": updated.isoformat(),
            "cash_balance": cash,
            "total_asset": 2_000_000,
            "stock_eval_amount": 500_000,
        }
    )


@pytest.mark.asyncio
async def test_read_snapshot_fresh():
    redis = _FakeRedis(_snapshot_json(age_sec=10))
    snap = await read_balance_snapshot(redis)
    assert snap is not None
    assert snap["cash_balance"] == 1_500_000
    assert snap["positions"][0]["stock_code"] == "005930"


@pytest.mark.asyncio
async def test_read_snapshot_too_old_returns_none():
    """monitor 가 죽어 스냅샷이 오래되면 None — 호출자가 HTTP fallback 으로."""
    redis = _FakeRedis(_snapshot_json(age_sec=700))
    snap = await read_balance_snapshot(redis, max_age_sec=600)
    assert snap is None


@pytest.mark.asyncio
async def test_read_snapshot_missing_returns_none():
    redis = _FakeRedis(None)
    assert await read_balance_snapshot(redis) is None


@pytest.mark.asyncio
async def test_read_snapshot_redis_error_fail_open():
    """Redis 장애 시 예외를 올리지 않고 None (fail-open)."""
    redis = _FakeRedis(raise_on_get=True)
    assert await read_balance_snapshot(redis) is None


@pytest.mark.asyncio
async def test_fetch_prefers_snapshot_no_http():
    """스냅샷이 있으면 HTTP 를 아예 부르지 않는다 — KIS 호출 절약이 목적."""
    redis = _FakeRedis(_snapshot_json())
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{GATEWAY}/api/balance")
        result = await get_balance_snapshot_or_fetch(redis, GATEWAY)

    assert result is not None
    assert result["cash_balance"] == 1_500_000
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_fetch_falls_back_to_http_when_no_snapshot():
    """스냅샷이 없으면 gateway HTTP fallback (gateway 잔고 캐시가 보호)."""
    redis = _FakeRedis(None)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{GATEWAY}/api/balance").respond(
            200, json={"positions": [], "cash_balance": 999, "total_asset": 999}
        )
        result = await get_balance_snapshot_or_fetch(redis, GATEWAY)

    assert result is not None
    assert result["cash_balance"] == 999


@pytest.mark.asyncio
async def test_fetch_returns_none_when_both_fail():
    redis = _FakeRedis(None)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{GATEWAY}/api/balance").mock(side_effect=httpx.ConnectError("down"))
        result = await get_balance_snapshot_or_fetch(redis, GATEWAY)
    assert result is None


@pytest.mark.asyncio
async def test_fetch_works_without_redis_client():
    """redis_client=None (스냅샷 미지원 호출자) 도 HTTP 경로로 동작."""
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{GATEWAY}/api/balance").respond(200, json={"cash_balance": 1})
        result = await get_balance_snapshot_or_fetch(None, GATEWAY)
    assert result == {"cash_balance": 1}
