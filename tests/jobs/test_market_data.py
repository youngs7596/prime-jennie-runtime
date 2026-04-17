"""`refresh_market_caps` 스모크 — fake asyncpg pool + respx (KIS gateway 모킹).

v2 원본 상수: 상위 300 종목 + 18 req/sec 율제한. 테스트는 소규모로 2 종목만
시드해서 UPDATE 인자/market_cap 값 검증.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.market_data import refresh_market_caps


class _FakeConn:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        self.calls.append((sql, args))
        return self._rows

    async def execute(self, sql: str, *args: object) -> str:
        self.calls.append((sql, args))
        return "UPDATE 1"


class _FakePool:
    def __init__(self, rows: list[dict]) -> None:
        self.conn = _FakeConn(rows)

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_refresh_market_caps_updates_from_snapshot():
    pool = _FakePool(
        rows=[{"stock_code": "005930"}, {"stock_code": "000660"}]
    )
    base = "http://kis-gateway:8080"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{base}/api/snapshot/005930").respond(
            200, json={"stock_code": "005930", "market_cap": 420_000_000_000_000}
        )
        mock.get(f"{base}/api/snapshot/000660").respond(
            200, json={"stock_code": "000660", "market_cap": 100_000_000_000_000}
        )
        async with httpx.AsyncClient() as client:
            # rate_per_sec 큰 값으로 sleep 회피
            await refresh_market_caps(
                pool, client, base, top_n=2, rate_per_sec=1000.0
            )

    updates = [c for c in pool.conn.calls if "UPDATE stock_masters" in c[0]]
    assert len(updates) == 2
    mapped = {c[1][1]: c[1][0] for c in updates}
    assert mapped["005930"] == 420_000_000_000_000
    assert mapped["000660"] == 100_000_000_000_000


@pytest.mark.asyncio
async def test_refresh_market_caps_skips_zero_or_missing():
    pool = _FakePool(
        rows=[{"stock_code": "005930"}, {"stock_code": "000660"}]
    )
    base = "http://kis-gateway:8080"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{base}/api/snapshot/005930").respond(
            200, json={"stock_code": "005930", "market_cap": 0}
        )
        mock.get(f"{base}/api/snapshot/000660").respond(
            200, json={"stock_code": "000660"}  # missing market_cap
        )
        async with httpx.AsyncClient() as client:
            await refresh_market_caps(
                pool, client, base, top_n=2, rate_per_sec=1000.0
            )

    updates = [c for c in pool.conn.calls if "UPDATE stock_masters" in c[0]]
    assert updates == []


@pytest.mark.asyncio
async def test_refresh_market_caps_tolerates_gateway_errors():
    pool = _FakePool(
        rows=[{"stock_code": "005930"}, {"stock_code": "000660"}]
    )
    base = "http://kis-gateway:8080"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{base}/api/snapshot/005930").respond(500)
        mock.get(f"{base}/api/snapshot/000660").respond(
            200, json={"stock_code": "000660", "market_cap": 100}
        )
        async with httpx.AsyncClient() as client:
            await refresh_market_caps(
                pool, client, base, top_n=2, rate_per_sec=1000.0
            )

    updates = [c for c in pool.conn.calls if "UPDATE stock_masters" in c[0]]
    assert len(updates) == 1
    assert updates[0][1][1] == "000660"
