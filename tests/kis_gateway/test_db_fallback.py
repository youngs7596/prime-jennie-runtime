"""FallbackPriceService — KIS 실패 시 repo 조회, 성공 시 cache write."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock

import pytest
from minyoung_mah import CollectingObserver

from prime_jennie_runtime.kis_gateway.circuit_breaker import (
    AsyncCircuitBreaker,
    CircuitBreakerError,
)
from prime_jennie_runtime.kis_gateway.db_fallback import FallbackPriceService
from prime_jennie_runtime.kis_gateway.kis_api import KISApiError
from prime_jennie_runtime.kis_gateway.price_repo import InMemoryPriceRepo
from prime_jennie_runtime.kis_gateway.schemas import DailyPrice, MinutePrice


def _d(code: str = "005930", close: int = 100, d: str = "2026-04-15") -> DailyPrice:
    return DailyPrice(
        stock_code=code,
        price_date=date.fromisoformat(d),
        open_price=close,
        high_price=close + 1,
        low_price=close - 1,
        close_price=close,
        volume=1_000,
    )


def _m(code: str = "005930", close: int = 100, ts: str = "2026-04-15T09:00:00") -> MinutePrice:
    return MinutePrice(
        stock_code=code,
        price_datetime=datetime.fromisoformat(ts),
        open_price=close,
        high_price=close + 1,
        low_price=close - 1,
        close_price=close,
        volume=500,
    )


def _kis_stub(daily=None, minute=None, daily_exc=None, minute_exc=None):
    api = AsyncMock()
    if daily_exc is not None:
        api.get_daily_prices.side_effect = daily_exc
    else:
        api.get_daily_prices.return_value = daily or []
    if minute_exc is not None:
        api.get_minute_prices.side_effect = minute_exc
    else:
        api.get_minute_prices.return_value = minute or []
    return api


def _closed_circuit() -> AsyncCircuitBreaker:
    return AsyncCircuitBreaker(fail_max=100, reset_sec=60)


# ---------- happy path: cache write on success ----------


@pytest.mark.asyncio
async def test_daily_happy_path_writes_cache():
    repo = InMemoryPriceRepo()
    api = _kis_stub(daily=[_d(close=101), _d(close=102, d="2026-04-14")])
    observer = CollectingObserver()
    service = FallbackPriceService(api, _closed_circuit(), repo, observer=observer)

    rows = await service.get_daily_prices("005930", 30)
    assert len(rows) == 2
    # repo 에도 쓰였는지
    cached = await repo.fetch_daily("005930", 30)
    assert len(cached) == 2
    names = {e.name for e in observer.events}
    assert "pj.kis.cache_write" in names


# ---------- KIS API 실패 + repo 데이터 있음 → fallback_hit ----------


@pytest.mark.asyncio
async def test_daily_kis_error_falls_back_to_repo():
    repo = InMemoryPriceRepo()
    await repo.upsert_daily([_d(close=42)])
    api = _kis_stub(daily_exc=KISApiError("rate limited"))
    observer = CollectingObserver()
    service = FallbackPriceService(api, _closed_circuit(), repo, observer=observer)

    rows = await service.get_daily_prices("005930", 5)
    assert len(rows) == 1
    assert rows[0].close_price == 42
    names = {e.name for e in observer.events}
    assert "pj.kis.fallback_hit" in names


# ---------- circuit open + repo empty → 원 예외 재발생 ----------


@pytest.mark.asyncio
async def test_daily_circuit_open_and_empty_repo_reraises():
    repo = InMemoryPriceRepo()
    api = _kis_stub(daily_exc=CircuitBreakerError("open"))
    observer = CollectingObserver()
    service = FallbackPriceService(api, _closed_circuit(), repo, observer=observer)

    with pytest.raises(CircuitBreakerError):
        await service.get_daily_prices("005930", 5)
    names = {e.name for e in observer.events}
    assert "pj.kis.fallback_miss" in names


# ---------- minute happy path ----------


@pytest.mark.asyncio
async def test_minute_happy_path_writes_cache():
    repo = InMemoryPriceRepo()
    api = _kis_stub(minute=[_m(close=101), _m(close=102, ts="2026-04-15T09:01:00")])
    observer = CollectingObserver()
    service = FallbackPriceService(api, _closed_circuit(), repo, observer=observer)

    rows = await service.get_minute_prices("005930")
    assert len(rows) == 2
    cached = await repo.fetch_minute("005930")
    assert len(cached) == 2


@pytest.mark.asyncio
async def test_minute_kis_error_falls_back():
    repo = InMemoryPriceRepo()
    await repo.upsert_minute([_m(close=99)])
    api = _kis_stub(minute_exc=KISApiError("temporary"))
    observer = CollectingObserver()
    service = FallbackPriceService(api, _closed_circuit(), repo, observer=observer)

    rows = await service.get_minute_prices("005930")
    assert len(rows) == 1
    assert rows[0].close_price == 99


# ---------- happy path 에 빈 응답 → cache write 건수 0, 에러 없음 ----------


@pytest.mark.asyncio
async def test_daily_empty_kis_response_no_cache_write():
    repo = InMemoryPriceRepo()
    api = _kis_stub(daily=[])
    observer = CollectingObserver()
    service = FallbackPriceService(api, _closed_circuit(), repo, observer=observer)

    rows = await service.get_daily_prices("005930", 30)
    assert rows == []
    # cache_write 이벤트 없음 (빈 응답 upsert 스킵)
    names = [e.name for e in observer.events]
    assert "pj.kis.cache_write" not in names
