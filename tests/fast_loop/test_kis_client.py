"""KisClient 테스트 — respx로 KIS Gateway 엔드포인트 mocking."""

from __future__ import annotations

import httpx
import pytest
import respx

from prime_jennie_runtime.fast_loop.kis_client import KisClient
from prime_jennie_runtime.infra.config import KISConfig
from prime_jennie_runtime.kis_gateway.schemas import OrderRequest


def _config() -> KISConfig:
    return KISConfig(gateway_url="http://gateway.test")


@pytest.mark.asyncio
async def test_buy_success():
    async with respx.mock(base_url="http://gateway.test") as mock:
        mock.post("/api/order/buy").mock(
            return_value=httpx.Response(
                200,
                json={
                    "success": True,
                    "order_no": "BUY123",
                    "stock_code": "005930",
                    "quantity": 10,
                    "price": 70000,
                    "message": None,
                },
            )
        )
        client = KisClient(_config())
        result = await client.buy(
            OrderRequest(stock_code="005930", quantity=10, order_type="market")
        )
        await client.close()
        assert result.success is True
        assert result.order_no == "BUY123"


@pytest.mark.asyncio
async def test_confirm_order_filled():
    async with respx.mock(base_url="http://gateway.test") as mock:
        mock.get("/api/order/BUY123").mock(
            return_value=httpx.Response(
                200, json={"filled": True, "filled_qty": 10, "avg_price": 70000.0}
            )
        )
        client = KisClient(_config())
        status = await client.confirm_order("BUY123", max_retries=1, interval=0.0)
        await client.close()
        assert status is not None
        assert status.filled is True
        assert status.filled_qty == 10


@pytest.mark.asyncio
async def test_cancel_order():
    async with respx.mock(base_url="http://gateway.test") as mock:
        mock.post("/api/order/cancel").mock(
            return_value=httpx.Response(200, json={"success": True})
        )
        client = KisClient(_config())
        ok = await client.cancel_order("BUY123")
        await client.close()
        assert ok is True


@pytest.mark.asyncio
async def test_subscribe():
    async with respx.mock(base_url="http://gateway.test") as mock:
        mock.post("/api/realtime/subscribe").mock(
            return_value=httpx.Response(200, json={"success": True})
        )
        client = KisClient(_config())
        await client.subscribe(["005930"])
        await client.close()
