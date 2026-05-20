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


def _balance_payload_q(quantities: dict[str, int]):
    """{ticker: quantity} 기반 balance payload — 수량 drift 테스트용."""
    return {
        "cash_balance": 1000,
        "total_asset": 1000,
        "stock_eval_amount": 0,
        "positions": [
            {
                "stock_code": c,
                "stock_name": c,
                "quantity": q,
                "average_buy_price": 100,
                "total_buy_amount": 100 * q,
            }
            for c, q in quantities.items()
        ],
        "position_count": len(quantities),
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
        assert result == {"only_in_state": [], "only_in_kis": [], "qty_mismatch": []}
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
        assert result == {
            "only_in_state": ["128940"],
            "only_in_kis": [],
            "qty_mismatch": [],
        }
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
        assert result == {
            "only_in_state": [],
            "only_in_kis": ["066970"],
            "qty_mismatch": [],
        }
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
        assert result == {"only_in_state": [], "only_in_kis": [], "qty_mismatch": []}
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_qty_mismatch_state_overcount_emits_critical():
    """양쪽에 ticker 가 다 있지만 v3 state(61) > KIS(41) → 외부 일부 매도.
    v3 가 실제보다 많이 보유 중이라 착각 → critical."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        await redis.set(f"{POSITION_STATE_PREFIX}ps_a", _state("241560", qty=61))
        with respx.mock(assert_all_called=False) as mock:
            mock.get("http://kis:8080/api/balance").mock(
                return_value=httpx.Response(200, json=_balance_payload_q({"241560": 41}))
            )
            async with httpx.AsyncClient() as client:
                result = await reconcile_state_kis(
                    redis_client=redis, http=client, kis_gateway_url="http://kis:8080"
                )
        assert result == {
            "only_in_state": [],
            "only_in_kis": [],
            "qty_mismatch": [{"ticker": "241560", "state_qty": 61, "kis_qty": 41}],
        }
        msgs = await redis.xrange("v3:notifications")
        assert len(msgs) == 1
        payload = json.loads(msgs[0][1][b"payload"])
        assert payload["severity"] == "critical"
        assert "241560(state=61 kis=41)" in payload["body"]
        assert payload["metadata"]["qty_mismatch"] == [
            {"ticker": "241560", "state_qty": 61, "kis_qty": 41}
        ]
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_qty_mismatch_state_undercount_emits_warning():
    """v3 state(41) < KIS(61) → 외부 일부 매수. v3 과소 카운트 → warning."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        await redis.set(f"{POSITION_STATE_PREFIX}ps_a", _state("241560", qty=41))
        with respx.mock(assert_all_called=False) as mock:
            mock.get("http://kis:8080/api/balance").mock(
                return_value=httpx.Response(200, json=_balance_payload_q({"241560": 61}))
            )
            async with httpx.AsyncClient() as client:
                result = await reconcile_state_kis(
                    redis_client=redis, http=client, kis_gateway_url="http://kis:8080"
                )
        assert result["qty_mismatch"] == [{"ticker": "241560", "state_qty": 41, "kis_qty": 61}]
        msgs = await redis.xrange("v3:notifications")
        assert len(msgs) == 1
        payload = json.loads(msgs[0][1][b"payload"])
        assert payload["severity"] == "warning"
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_multiple_state_keys_for_same_ticker_summed():
    """같은 ticker 의 여러 position_state key 는 합산 — manual sync key + v3 sheet key.
    800 + 798 = 1598 == KIS 1598 → aligned (alert 0)."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        await redis.set(f"{POSITION_STATE_PREFIX}ps_a", _state("069500", sheet_id="ps_a", qty=800))
        await redis.set(
            f"{POSITION_STATE_PREFIX}manual_069500", _state("069500", sheet_id="m", qty=798)
        )
        with respx.mock(assert_all_called=False) as mock:
            mock.get("http://kis:8080/api/balance").mock(
                return_value=httpx.Response(200, json=_balance_payload_q({"069500": 1598}))
            )
            async with httpx.AsyncClient() as client:
                result = await reconcile_state_kis(
                    redis_client=redis, http=client, kis_gateway_url="http://kis:8080"
                )
        assert result == {"only_in_state": [], "only_in_kis": [], "qty_mismatch": []}
        assert await redis.xlen("v3:notifications") == 0
    finally:
        await redis.aclose()
