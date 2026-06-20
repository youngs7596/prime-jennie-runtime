"""KIS Gateway HTTP Client — async.

v2 `prime_jennie/infra/kis/client.py` (212줄) 포팅. httpx.Client → httpx.AsyncClient.
Fast loop entry/exit executor가 Gateway 컨테이너의 REST 엔드포인트 호출.

Gateway 스키마는 `kis_gateway/schemas.py`에 통합됨 (OrderRequest/Result 등).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from prime_jennie_runtime.infra.config import KISConfig
from prime_jennie_runtime.kis_gateway.schemas import (
    DailyExecution,
    OrderRequest,
    OrderResult,
    OrderStatusResult,
    PortfolioState,
    StockSnapshot,
)

logger = logging.getLogger(__name__)


class KisClient:
    """KIS Gateway HTTP 클라이언트 (async).

    Usage:
        async with KisClient(config) as client:
            result = await client.buy(OrderRequest(stock_code="005930", quantity=10))
            balance = await client.get_balance()
    """

    def __init__(
        self,
        config: KISConfig,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ):
        self._base_url = config.gateway_url.rstrip("/")
        self._timeout = timeout
        self._owned = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )

    async def buy(self, order: OrderRequest) -> OrderResult:
        """매수 주문."""
        resp = await self._client.post("/api/order/buy", json=order.model_dump())
        resp.raise_for_status()
        return OrderResult.model_validate(resp.json())

    async def sell(self, order: OrderRequest) -> OrderResult:
        """매도 주문."""
        resp = await self._client.post("/api/order/sell", json=order.model_dump())
        resp.raise_for_status()
        return OrderResult.model_validate(resp.json())

    async def cancel_order(self, order_no: str) -> bool:
        """주문 취소."""
        resp = await self._client.post("/api/order/cancel", json={"order_no": order_no})
        resp.raise_for_status()
        return resp.json().get("success", False)

    async def check_order_status(self, order_no: str) -> OrderStatusResult | None:
        """주문 체결 상태 조회. 실패 시 None."""
        try:
            resp = await self._client.get(f"/api/order/{order_no}")
            resp.raise_for_status()
            return OrderStatusResult.model_validate(resp.json())
        except httpx.HTTPError as e:
            logger.error("check_order_status failed: %s", e)
            return None

    async def confirm_order(
        self,
        order_no: str,
        *,
        max_retries: int = 5,
        interval: float = 3.0,
    ) -> OrderStatusResult | None:
        """주문 체결 폴링.

        전량 미체결이라도 부분 체결(filled_qty > 0)이면 성공 반환.
        """
        last: OrderStatusResult | None = None
        for attempt in range(max_retries):
            status = await self.check_order_status(order_no)
            if status:
                last = status
                if status.filled:
                    return status
            if attempt < max_retries - 1:
                await asyncio.sleep(interval)

        if last and last.filled_qty > 0:
            logger.warning(
                "Order %s partial fill: qty=%d avg=%s",
                order_no,
                last.filled_qty,
                last.avg_price,
            )
            return last

        logger.warning("Order %s not filled after %d retries", order_no, max_retries)
        return None

    async def get_balance(self) -> PortfolioState:
        """잔고 조회."""
        resp = await self._client.get("/api/balance")
        resp.raise_for_status()
        return PortfolioState.model_validate(resp.json())

    async def get_cash(self) -> int:
        """현금 잔고."""
        resp = await self._client.get("/api/cash")
        resp.raise_for_status()
        return int(resp.json().get("cash_balance", 0))

    async def get_snapshot(self, stock_code: str) -> StockSnapshot:
        """현재가 스냅샷."""
        resp = await self._client.get(f"/api/snapshot/{stock_code}")
        resp.raise_for_status()
        return StockSnapshot.model_validate(resp.json())

    async def get_daily_executions(
        self, *, start_date: str, end_date: str, stock_code: str = ""
    ) -> list[DailyExecution]:
        """일별 주문체결 조회 (YYYYMMDD). crash 복구 시 KIS 원장 대조용."""
        params: dict[str, str] = {"start_date": start_date, "end_date": end_date}
        if stock_code:
            params["stock_code"] = stock_code
        resp = await self._client.get("/api/executions", params=params)
        resp.raise_for_status()
        return [DailyExecution.model_validate(r) for r in resp.json()]

    async def subscribe(self, codes: list[str]) -> dict[str, Any]:
        """실시간 구독 요청."""
        resp = await self._client.post("/api/realtime/subscribe", json={"codes": codes})
        resp.raise_for_status()
        return resp.json()

    async def unsubscribe(self, codes: list[str]) -> dict[str, Any]:
        """실시간 구독 해제."""
        resp = await self._client.post("/api/realtime/unsubscribe", json={"codes": codes})
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def __aenter__(self) -> KisClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
