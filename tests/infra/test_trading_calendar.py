"""trading_calendar 가드 회귀 테스트.

휴장일에 slow_loop / job_worker 잡이 도는 사고 (2026-05-25) 재발 방지.
주말은 HTTP 호출 없이 즉시 False, 평일은 gateway 응답 session 으로 판정,
HTTP 실패 시 거래일 가정 (안전 fallback).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from prime_jennie_runtime.infra import trading_calendar
from prime_jennie_runtime.infra.trading_calendar import is_trading_day_via_gateway


_GATEWAY = "http://kis-gateway:8080"
_KST = timezone(timedelta(hours=9))


@pytest.fixture(autouse=True)
def _reset_cache():
    trading_calendar._reset_cache_for_testing()
    yield
    trading_calendar._reset_cache_for_testing()


def _mock_client(handler) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


async def test_weekday_holiday_session_returns_false(monkeypatch):
    monkeypatch.setattr(
        trading_calendar,
        "datetime",
        _frozen_datetime(datetime(2026, 5, 25, 10, 0, tzinfo=_KST)),
    )
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(200, json={"is_open": False, "session": "holiday"})

    async with _mock_client(handler) as http:
        result = await is_trading_day_via_gateway(_GATEWAY, http=http)

    assert result is False
    assert len(calls) == 1


async def test_weekday_regular_session_returns_true(monkeypatch):
    monkeypatch.setattr(
        trading_calendar,
        "datetime",
        _frozen_datetime(datetime(2026, 5, 26, 10, 0, tzinfo=_KST)),
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"is_open": True, "session": "regular"})

    async with _mock_client(handler) as http:
        result = await is_trading_day_via_gateway(_GATEWAY, http=http)

    assert result is True


async def test_weekend_short_circuits_without_http(monkeypatch):
    monkeypatch.setattr(
        trading_calendar,
        "datetime",
        _frozen_datetime(datetime(2026, 5, 23, 10, 0, tzinfo=_KST)),  # 토요일
    )
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(200, json={"is_open": False, "session": "holiday"})

    async with _mock_client(handler) as http:
        result = await is_trading_day_via_gateway(_GATEWAY, http=http)

    assert result is False
    assert calls == []


async def test_http_failure_falls_back_to_trading_day(monkeypatch):
    monkeypatch.setattr(
        trading_calendar,
        "datetime",
        _frozen_datetime(datetime(2026, 5, 26, 10, 0, tzinfo=_KST)),
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with _mock_client(handler) as http:
        result = await is_trading_day_via_gateway(_GATEWAY, http=http)

    assert result is True


async def test_result_is_cached_per_day(monkeypatch):
    monkeypatch.setattr(
        trading_calendar,
        "datetime",
        _frozen_datetime(datetime(2026, 5, 25, 10, 0, tzinfo=_KST)),
    )
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(200, json={"is_open": False, "session": "holiday"})

    async with _mock_client(handler) as http:
        await is_trading_day_via_gateway(_GATEWAY, http=http)
        await is_trading_day_via_gateway(_GATEWAY, http=http)
        await is_trading_day_via_gateway(_GATEWAY, http=http)

    assert len(calls) == 1


def _frozen_datetime(fixed: datetime):
    """datetime.now(tz) 만 고정한 stub. 다른 메서드는 원본 datetime 위임."""

    class _D(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz is None:
                return fixed.replace(tzinfo=None)
            return fixed.astimezone(tz)

    return _D
