"""네이버 시장 크롤러 스모크 — fchart XML / 투자자 동향 HTML 고정."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.crawlers.naver_market import (
    fetch_index_daily_prices,
    fetch_investor_flows,
)

_FCHART_URL_RE = r"https://fchart\.stock\.naver\.com/sise\.nhn.*"
_INVESTOR_URL_RE = r"https://finance\.naver\.com/sise/investorDealTrendDay\.naver.*"


_FCHART_XML = """
<protocol>
  <chartdata symbol="KOSPI">
    <item data="20260101|3000.00|3050.00|2990.00|3010.50|1234567" />
    <item data="20260102|3010.50|3080.00|3000.00|3070.25|2345678" />
    <item data="bad" />
  </chartdata>
</protocol>
"""

_INVESTOR_HTML = """
<html><body>
<table class="type_1">
  <tr>
    <th>날짜</th><th>외국인(억)</th><th>기관계</th><th>개인</th>
  </tr>
  <tr>
    <td>26.02.27</td><td>+1,500</td><td>-800</td><td>-700</td>
  </tr>
  <tr>
    <td>26.02.26</td><td>200</td><td>100</td><td>-300</td>
  </tr>
</table>
</body></html>
"""


@pytest.mark.asyncio
async def test_fetch_index_daily_prices_parses_and_sorts():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_FCHART_URL_RE).respond(200, text=_FCHART_XML)
        async with httpx.AsyncClient() as client:
            bars = await fetch_index_daily_prices(client, "KOSPI", count=5)
    assert len(bars) == 2
    assert bars[0].price_date == date(2026, 1, 1)
    assert bars[1].price_date == date(2026, 1, 2)
    assert bars[0].open_price == 3000.0
    assert bars[1].volume == 2345678


@pytest.mark.asyncio
async def test_fetch_investor_flows_parses_target_date():
    # 크롤러가 resp.encoding="euc-kr" 강제라서 응답 body 도 euc-kr bytes 로 내려야 일치
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_INVESTOR_URL_RE).respond(200, content=_INVESTOR_HTML.encode("euc-kr"))
        async with httpx.AsyncClient() as client:
            flows = await fetch_investor_flows(client, "kospi", "20260227")
    assert flows is not None
    assert flows.trade_date == date(2026, 2, 27)
    assert flows.foreign_net == 1500.0
    assert flows.institutional_net == -800.0
    assert flows.retail_net == -700.0


@pytest.mark.asyncio
async def test_fetch_investor_flows_returns_none_on_missing_date():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_INVESTOR_URL_RE).respond(200, content=_INVESTOR_HTML.encode("euc-kr"))
        async with httpx.AsyncClient() as client:
            flows = await fetch_investor_flows(client, "kospi", "20260101")
    assert flows is None
