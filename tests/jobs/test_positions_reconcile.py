"""positions_reconcile — state ↔ KIS balance read-only diff."""

from __future__ import annotations

import json

import fakeredis.aioredis
import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.positions_reconcile import (
    POSITION_STATE_PREFIX,
    reconcile_state_kis,
)


def _balance_payload(stock_codes: list[str]):
    return {
        "cash_balance": 1000,
        "total_asset": 1000,
        "stock_eval_amount": 0,
        "positions": [
            {
                "stock_code": c,
                "stock_name": c,
                "quantity": 10,
                "average_buy_price": 100,
                "total_buy_amount": 1000,
            }
            for c in stock_codes
        ],
        "position_count": len(stock_codes),
        "timestamp": "2026-05-15T10:00:00+09:00",
    }


def _state(ticker: str, sheet_id: str = "ps_test_a", qty: int = 10) -> str:
    return json.dumps({"sheet_id": sheet_id, "ticker": ticker, "quantity": qty})


@pytest.mark.asyncio
async def test_aligned_no_alert():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        await redis.set(f"{POSITION_STATE_PREFIX}ps_a", _state("005930"))
        with respx.mock(assert_all_called=False) as mock:
            mock.get("http://kis:8080/api/balance").mock(
                return_value=httpx.Response(200, json=_balance_payload(["005930"]))
            )
            async with httpx.AsyncClient() as client:
                result = await reconcile_state_kis(
                    redis_client=redis, http=client, kis_gateway_url="http://kis:8080"
                )
        assert result == {"only_in_state": [], "only_in_kis": []}
        # alert publish 0
        assert await redis.xlen("v3:notifications") == 0
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_only_in_state_emits_critical_alert():
    """v3 state 에 있는데 KIS 잔량 없음 → 외부 매도. critical."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        await redis.set(f"{POSITION_STATE_PREFIX}ps_a", _state("128940"))
        with respx.mock(assert_all_called=False) as mock:
            mock.get("http://kis:8080/api/balance").mock(
                return_value=httpx.Response(200, json=_balance_payload([]))
            )
            async with httpx.AsyncClient() as client:
                result = await reconcile_state_kis(
                    redis_client=redis, http=client, kis_gateway_url="http://kis:8080"
                )
        assert result == {"only_in_state": ["128940"], "only_in_kis": []}
        # alert 1건 publish
        msgs = await redis.xrange("v3:notifications")
        assert len(msgs) == 1
        payload = json.loads(msgs[0][1][b"payload"])
        assert payload["severity"] == "critical"
        assert "128940" in payload["body"]
        assert payload["metadata"]["only_in_state"] == ["128940"]
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_only_in_kis_emits_warning_alert():
    """KIS 잔량은 있는데 v3 state 없음 → 외부 매수. warning."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock.get("http://kis:8080/api/balance").mock(
                return_value=httpx.Response(200, json=_balance_payload(["066970"]))
            )
            async with httpx.AsyncClient() as client:
                result = await reconcile_state_kis(
                    redis_client=redis, http=client, kis_gateway_url="http://kis:8080"
                )
        assert result == {"only_in_state": [], "only_in_kis": ["066970"]}
        msgs = await redis.xrange("v3:notifications")
        assert len(msgs) == 1
        payload = json.loads(msgs[0][1][b"payload"])
        assert payload["severity"] == "warning"
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_balance_fetch_failure_fails_open():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock.get("http://kis:8080/api/balance").mock(side_effect=httpx.ConnectError("boom"))
            async with httpx.AsyncClient() as client:
                result = await reconcile_state_kis(
                    redis_client=redis, http=client, kis_gateway_url="http://kis:8080"
                )
        assert result == {}
        assert await redis.xlen("v3:notifications") == 0
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_zero_quantity_state_ignored():
    """quantity=0 state 는 ignore (이미 closed)."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        await redis.set(f"{POSITION_STATE_PREFIX}ps_a", _state("005930", qty=0))
        with respx.mock(assert_all_called=False) as mock:
            mock.get("http://kis:8080/api/balance").mock(
                return_value=httpx.Response(200, json=_balance_payload([]))
            )
            async with httpx.AsyncClient() as client:
                result = await reconcile_state_kis(
                    redis_client=redis, http=client, kis_gateway_url="http://kis:8080"
                )
        assert result == {"only_in_state": [], "only_in_kis": []}
    finally:
        await redis.aclose()
