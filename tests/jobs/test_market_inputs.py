"""`collect_vkospi` + `collect_market_investor_flows` 스모크 — HTTP 모킹 + fake pool."""

from __future__ import annotations

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.market_inputs import (
    collect_market_investor_flows,
    collect_vkospi,
)

from .naver_trend_fixture import INVESTOR_TREND_URL_RE, trend_json, trend_row

_CNBC_URL_RE = r"https://ts-api\.cnbc\.com/harmony/app/charts/.*"

_CNBC_JSON = {
    "barData": {
        "priceBars": [
            {
                "open": "25.0",
                "high": "26.0",
                "low": "24.0",
                "close": "25.76",
                "volume": 0,
                "tradeTime": "20260101000000",
            },
            {
                "open": "88.0",
                "high": "90.0",
                "low": "85.0",
                "close": "89.41",
                "volume": 0,
                "tradeTime": "20260623000000",
            },
            {
                "open": "1",
                "high": "1",
                "low": "1",
                "close": "9999",  # 범위 밖 → sanity drop
                "volume": 0,
                "tradeTime": "20260102000000",
            },
        ]
    }
}


class _FakeConn:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args: object) -> str:
        self.execute_calls.append((sql, args))
        return "INSERT 0 1"


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_collect_vkospi_upserts_valid_bars_only():
    pool = _FakePool()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_CNBC_URL_RE).respond(200, json=_CNBC_JSON)
        async with httpx.AsyncClient() as client:
            stats = await collect_vkospi(pool, client, range_token="1M")
    assert stats == {"upserted": 2, "dropped": 1}  # 9999 는 sanity drop
    inserts = [c for c in pool.conn.execute_calls if "INSERT INTO vkospi_daily" in c[0]]
    assert len(inserts) == 2
    closes = {c[1][4] for c in inserts}  # close_price 위치
    assert closes == {25.76, 89.41}


@pytest.mark.asyncio
async def test_collect_market_investor_flows_upserts_pension():
    pool = _FakePool()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=INVESTOR_TREND_URL_RE).respond(
            200, json=trend_json(trend_row("20260623"))
        )
        async with httpx.AsyncClient() as client:
            stats = await collect_market_investor_flows(pool, client)
    assert stats == {"upserted": 1}
    inserts = [c for c in pool.conn.execute_calls if "INSERT INTO market_investor_flows" in c[0]]
    assert len(inserts) == 1
    # 단위 라벨은 억원(eok_krw) — 출처는 원 단위라 크롤러가 억원으로 바꿔 준다.
    # 'million_krw' 로 잘못 박혀 100배 과소였던 것을 2026-06-26 정정.
    assert "'eok_krw'" in inserts[0][0]
    assert "'million_krw'" not in inserts[0][0]
    args = inserts[0][1]
    assert args[1] == "KOSPI"
    assert args[2] == 85910.0  # individual_net
    assert args[10] == -2937.0  # pension_net (연기금등)
