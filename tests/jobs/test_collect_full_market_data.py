"""`collect_full_market_data` 스모크 — top300 자동 발견 + gateway daily upsert."""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.full_market_daily import collect_full_market_data

GATEWAY = "http://kis-gateway:8080"
_DAILY_RE = rf"{GATEWAY}/api/market/daily-prices"


class _FakeConn:
    def __init__(self, codes: list[str]) -> None:
        self.codes = codes
        self.executemany_calls: list[tuple[str, list[tuple]]] = []
        self.master_query: str | None = None

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        if "FROM stock_masters" in sql:
            self.master_query = sql
            return [{"stock_code": c} for c in self.codes]
        return []

    async def execute(self, sql: str, *args: object) -> str:
        return "OK"

    async def executemany(self, sql: str, args: list[tuple]) -> None:
        self.executemany_calls.append((sql, args))


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _daily_payload(code: str, n: int = 5) -> list[dict]:
    return [
        {
            "stock_code": code,
            "price_date": (date.today() - timedelta(days=i)).isoformat(),
            "open_price": 1000,
            "high_price": 1010,
            "low_price": 990,
            "close_price": 1005,
            "volume": 100_000,
            "change_pct": 0.5,
        }
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_collect_full_market_data_upserts_for_all_targets():
    conn = _FakeConn(["005930", "000660", "035720"])
    pool = _FakePool(conn)

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=_DAILY_RE).respond(200, json=_daily_payload("X", n=5))
        async with httpx.AsyncClient() as client:
            result = await collect_full_market_data(pool, client, GATEWAY, top_n=3, days=30)

    assert result["target"] == 3
    assert result["upserted"] == 15  # 3 종목 × 5 봉
    assert result["failed"] == 0
    assert result["empty"] == 0
    assert len(conn.executemany_calls) == 3
    # universe SELECT 는 ETN 을 제외해야 한다 (2026-05-22 fix)
    assert conn.master_query is not None
    assert "NOT ILIKE '%ETN%'" in conn.master_query


@pytest.mark.asyncio
async def test_collect_full_market_data_handles_empty_universe():
    conn = _FakeConn([])
    pool = _FakePool(conn)

    async with httpx.AsyncClient() as client:
        result = await collect_full_market_data(pool, client, GATEWAY, top_n=300)

    assert result == {"target": 0, "upserted": 0, "failed": 0, "empty": 0}


@pytest.mark.asyncio
async def test_collect_full_market_data_continues_on_failure():
    conn = _FakeConn(["A", "B"])
    pool = _FakePool(conn)

    call = {"n": 0}

    def _side(req):
        call["n"] += 1
        if call["n"] == 1:
            return httpx.Response(503, json={"error": "gateway down"})
        return httpx.Response(200, json=_daily_payload("B", n=2))

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=_DAILY_RE).mock(side_effect=_side)
        async with httpx.AsyncClient() as client:
            result = await collect_full_market_data(pool, client, GATEWAY, top_n=2)

    assert result["failed"] == 1
    assert result["upserted"] == 2


@pytest.mark.asyncio
async def test_collect_full_market_data_counts_empty_responses():
    """HTTP 200 + 빈 리스트 응답은 failed 가 아니라 empty 로 집계된다.

    ETN 처럼 KIS 일봉 endpoint 가 에러 없이 빈 결과를 주는 종목 — failed=0 이
    실태를 가리지 않도록 별도 카운트.
    """
    conn = _FakeConn(["A", "B"])
    pool = _FakePool(conn)

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=_DAILY_RE).respond(200, json=[])
        async with httpx.AsyncClient() as client:
            result = await collect_full_market_data(pool, client, GATEWAY, top_n=2)

    assert result["target"] == 2
    assert result["failed"] == 0
    assert result["empty"] == 2
    assert result["upserted"] == 0
