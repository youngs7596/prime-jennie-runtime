"""`collect_vkospi` + `collect_market_investor_flows` 스모크 — HTTP 모킹 + fake pool."""

from __future__ import annotations

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.market_inputs import (
    collect_market_investor_flows,
    collect_vkospi,
)

_CNBC_URL_RE = r"https://ts-api\.cnbc\.com/harmony/app/charts/.*"
_INVESTOR_URL_RE = r"https://finance\.naver\.com/sise/investorDealTrendDay\.naver.*"

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

_BREAKDOWN_HTML = """
<html><body><table class="type_1">
  <tr><th rowspan="2">날짜</th><th rowspan="2">개인</th><th rowspan="2">외국인</th>
      <th rowspan="2">기관계</th><th colspan="6">기관</th><th rowspan="2">기타법인</th></tr>
  <tr><th>금융투자</th><th>보험</th><th>투신(사모)</th><th>은행</th>
      <th>기타금융기관</th><th>연기금등</th></tr>
  <tr><td>26.06.23</td><td>85,910</td><td>-42,047</td><td>-44,760</td>
      <td>-20,908</td><td>-1,018</td><td>-19,662</td><td>-165</td><td>-69</td>
      <td>-2,937</td><td>898</td></tr>
</table></body></html>
"""


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
        mock.get(url__regex=_INVESTOR_URL_RE).respond(200, content=_BREAKDOWN_HTML.encode("euc-kr"))
        async with httpx.AsyncClient() as client:
            stats = await collect_market_investor_flows(pool, client)
    assert stats == {"upserted": 1}
    inserts = [c for c in pool.conn.execute_calls if "INSERT INTO market_investor_flows" in c[0]]
    assert len(inserts) == 1
    # 단위 라벨은 억원(eok_krw) — 네이버 investorDealTrendDay 가 억원으로 준다.
    # 'million_krw' 로 잘못 박혀 100배 과소였던 것을 2026-06-26 정정.
    assert "'eok_krw'" in inserts[0][0]
    assert "'million_krw'" not in inserts[0][0]
    args = inserts[0][1]
    assert args[1] == "KOSPI"
    assert args[2] == 85910.0  # individual_net
    assert args[10] == -2937.0  # pension_net (연기금등)
