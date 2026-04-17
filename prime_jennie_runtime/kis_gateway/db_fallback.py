"""DB fallback 서비스 — KIS API 실패 시 PriceRepo 에서 최근 시세 반환.

흐름:
1. KIS API 호출 시도 (circuit_breaker 로 감싼 채로)
2. 성공 → 응답을 ``PriceRepo.upsert_*`` 로 read-through 캐시에 저장 후 반환
3. 실패 (``CircuitBreakerError`` / ``KISApiError``) → repo 에서 최근 데이터 조회
4. repo 도 비어있으면 원 예외를 그대로 재발생 (502/503 유지)

**Observer** 이벤트:
- ``pj.kis.fallback_hit`` — fallback 경로로 응답 반환 (repo rows 포함)
- ``pj.kis.fallback_miss`` — repo 도 비어있어 원 예외 재발생
- ``pj.kis.cache_write`` — 성공 시 upsert 에 걸린 row count
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from minyoung_mah import Observer

from prime_jennie_runtime.infra.observer_impl import pj_event

from .circuit_breaker import AsyncCircuitBreaker, CircuitBreakerError
from .kis_api import KISApi, KISApiError
from .price_repo import PriceRepo
from .schemas import DailyPrice, MinutePrice

logger = logging.getLogger(__name__)


class FallbackPriceService:
    """KIS API ↔ PriceRepo 사이를 조정."""

    def __init__(
        self,
        kis_api: KISApi,
        circuit: AsyncCircuitBreaker,
        repo: PriceRepo,
        *,
        observer: Observer | None = None,
    ) -> None:
        self._api = kis_api
        self._circuit = circuit
        self._repo = repo
        self._observer = observer

    async def get_daily_prices(self, stock_code: str, days: int) -> list[DailyPrice]:
        async def _call() -> list[DailyPrice]:
            return await self._circuit.call(self._api.get_daily_prices, stock_code, days)

        return await self._with_fallback(
            stock_code=stock_code,
            call=_call,
            fallback=lambda: self._repo.fetch_daily(stock_code, days),
            upsert=lambda rows: self._repo.upsert_daily(rows),  # type: ignore[arg-type]
            label="daily",
        )

    async def get_minute_prices(self, stock_code: str) -> list[MinutePrice]:
        async def _call() -> list[MinutePrice]:
            return await self._circuit.call(self._api.get_minute_prices, stock_code)

        return await self._with_fallback(
            stock_code=stock_code,
            call=_call,
            fallback=lambda: self._repo.fetch_minute(stock_code),
            upsert=lambda rows: self._repo.upsert_minute(rows),  # type: ignore[arg-type]
            label="minute",
        )

    async def _with_fallback(
        self,
        *,
        stock_code: str,
        call: Callable[[], Awaitable[list]],
        fallback: Callable[[], Awaitable[list]],
        upsert: Callable[[list], Awaitable[int]],
        label: str,
    ) -> list:
        try:
            rows = await call()
        except (CircuitBreakerError, KISApiError) as e:
            cached = await fallback()
            await self._emit_fallback(stock_code, label, cached, error=str(e))
            if not cached:
                raise
            return cached

        if rows:
            count = await upsert(rows)
            await self._emit(
                "pj.kis.cache_write",
                role="kis_gateway",
                ok=True,
                stock_code=stock_code,
                label=label,
                upserted=count,
            )
        return rows

    async def _emit_fallback(
        self, stock_code: str, label: str, cached: list, *, error: str
    ) -> None:
        if cached:
            await self._emit(
                "pj.kis.fallback_hit",
                role="kis_gateway",
                ok=True,
                stock_code=stock_code,
                label=label,
                rows=len(cached),
                error=error,
            )
        else:
            await self._emit(
                "pj.kis.fallback_miss",
                role="kis_gateway",
                ok=False,
                stock_code=stock_code,
                label=label,
                error=error,
            )

    async def _emit(self, name: str, **fields: object) -> None:
        if self._observer is None:
            return
        await self._observer.emit(pj_event(name, **fields))


__all__ = ["FallbackPriceService"]
