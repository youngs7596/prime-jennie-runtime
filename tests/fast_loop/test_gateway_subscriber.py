"""fast_loop.gateway_subscriber 스모크."""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest
import respx

from prime_jennie_runtime.fast_loop.gateway_subscriber import (
    load_subscription_codes,
    subscribe_on_startup,
)

GATEWAY = "http://kis-gateway:8080"
_SUB_RE = rf"{GATEWAY}/api/realtime/subscribe"


class _FakeConn:
    def __init__(
        self,
        positions: list[str],
        latest_date: date | None,
        watchlist: list[str],
    ) -> None:
        self._positions = positions
        self._latest_date = latest_date
        self._watchlist = watchlist

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        if "FROM positions" in sql:
            return [{"stock_code": c} for c in self._positions]
        if "FROM watchlist_histories" in sql and "WHERE snapshot_date" in sql:
            return [{"stock_code": c} for c in self._watchlist]
        raise AssertionError(f"unexpected SQL: {sql}")

    async def fetchval(self, sql: str, *args: object) -> Any:
        if "SELECT snapshot_date FROM watchlist_histories" in sql:
            return self._latest_date
        raise AssertionError(f"unexpected SQL: {sql}")


class _FakePool:
    def __init__(
        self,
        *,
        positions: list[str] | None = None,
        latest_date: date | None = None,
        watchlist: list[str] | None = None,
    ) -> None:
        self.conn = _FakeConn(positions or [], latest_date, watchlist or [])

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_load_subscription_codes_union_and_sorted():
    pool = _FakePool(
        positions=["005930", "000660"],
        latest_date=date(2026, 4, 24),
        watchlist=["005930", "035720", "068270"],
    )
    codes = await load_subscription_codes(pool)
    assert codes == ["000660", "005930", "035720", "068270"]


@pytest.mark.asyncio
async def test_load_subscription_codes_empty_everything():
    pool = _FakePool(positions=[], latest_date=None, watchlist=[])
    codes = await load_subscription_codes(pool)
    assert codes == []


@pytest.mark.asyncio
async def test_load_subscription_codes_positions_only_when_no_watchlist():
    pool = _FakePool(positions=["005930"], latest_date=None, watchlist=[])
    codes = await load_subscription_codes(pool)
    assert codes == ["005930"]


@pytest.mark.asyncio
async def test_subscribe_on_startup_posts_codes():
    pool = _FakePool(
        positions=["005930"],
        latest_date=date(2026, 4, 24),
        watchlist=["035720"],
    )
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(url__regex=_SUB_RE).respond(
            200,
            json={"added": ["005930", "035720"], "total_subscriptions": 2, "is_running": True},
        )
        result = await subscribe_on_startup(pool, GATEWAY)

    assert route.call_count == 1
    sent = route.calls[0].request
    body = sent.content.decode()
    assert "005930" in body and "035720" in body
    assert result["codes"] == ["005930", "035720"]
    assert result["response"]["is_running"] is True


@pytest.mark.asyncio
async def test_subscribe_on_startup_skips_when_no_codes():
    pool = _FakePool(positions=[], latest_date=None, watchlist=[])
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__regex=_SUB_RE)
        result = await subscribe_on_startup(pool, GATEWAY)

    assert route.call_count == 0
    assert result == {"codes": [], "skipped": True}


@pytest.mark.asyncio
async def test_subscribe_on_startup_swallows_gateway_error():
    """gateway 가 다운되어 있어도 fast_loop 기동을 막지 않아야 함."""
    pool = _FakePool(positions=["005930"], latest_date=None, watchlist=[])
    with respx.mock(assert_all_called=True) as mock:
        mock.post(url__regex=_SUB_RE).mock(side_effect=httpx.ConnectError("refused"))
        result = await subscribe_on_startup(pool, GATEWAY)

    assert result["codes"] == ["005930"]
    assert "error" in result
