"""네이버 시장 크롤러 스모크 — fchart XML / 투자자 동향 HTML 고정."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.crawlers.naver_market import (
    fetch_index_daily_prices,
    fetch_investor_flows,
    fetch_market_investor_breakdown,
)

_FCHART_URL_RE = r"https://fchart\.stock\.naver\.com/sise\.nhn.*"
_INVESTOR_URL_RE = r"https://finance\.naver\.com/sise/investorDealTrendDay\.naver.*"

# 실제 페이지 구조 재현: 2단 헤더(기관 colspan=6 그룹 + 하위 6컬럼), 연기금등 분리.
_BREAKDOWN_HTML = """
<html><body>
<table class="type_1">
  <tr>
    <th rowspan="2">날짜</th><th rowspan="2">개인</th><th rowspan="2">외국인</th>
    <th rowspan="2">기관계</th><th colspan="6">기관</th><th rowspan="2">기타법인</th>
  </tr>
  <tr>
    <th>금융투자</th><th>보험</th><th>투신(사모)</th><th>은행</th>
    <th>기타금융기관</th><th>연기금등</th>
  </tr>
  <tr>
    <td>26.06.23</td><td>85,910</td><td>-42,047</td><td>-44,760</td>
    <td>-20,908</td><td>-1,018</td><td>-19,662</td><td>-165</td><td>-69</td>
    <td>-2,937</td><td>898</td>
  </tr>
  <tr>
    <td>26.06.22</td><td>21,506</td><td>-25,466</td><td>3,034</td>
    <td>4,859</td><td>-860</td><td>1,038</td><td>-19</td><td>-150</td>
    <td>-1,834</td><td>926</td>
  </tr>
</table>
</body></html>
"""


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


@pytest.mark.asyncio
async def test_fetch_market_investor_breakdown_separates_pension():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_INVESTOR_URL_RE).respond(200, content=_BREAKDOWN_HTML.encode("euc-kr"))
        async with httpx.AsyncClient() as client:
            rows = await fetch_market_investor_breakdown(client, "kospi", "20260623")
    assert len(rows) == 2
    r = rows[0]
    assert r.trade_date == date(2026, 6, 23)
    assert r.market == "KOSPI"
    # 6-23 폭락일: 개인 매수 / 외국인·기관 매도 / 연기금도 순매도
    assert r.individual_net == 85910.0
    assert r.foreign_net == -42047.0
    assert r.institution_net == -44760.0
    assert r.pension_net == -2937.0  # 연기금등이 기관계와 별개로 분리됨
    assert r.financial_inv_net == -20908.0
    assert r.trust_net == -19662.0
    assert r.etc_corp_net == 898.0


@pytest.mark.asyncio
async def test_fetch_market_investor_breakdown_empty_when_no_pension_column():
    # 연기금 컬럼이 없으면(구조 변경) 빈 리스트로 안전 실패.
    html = '<html><body><table class="type_1">'
    html += "<tr><th>날짜</th><th>개인</th><th>외국인</th><th>기관계</th></tr>"
    html += "<tr><th>x</th></tr>"
    html += "<tr><td>26.06.23</td><td>1</td><td>2</td><td>3</td></tr></table></body></html>"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_INVESTOR_URL_RE).respond(200, content=html.encode("euc-kr"))
        async with httpx.AsyncClient() as client:
            rows = await fetch_market_investor_breakdown(client, "kospi", "20260623")
    assert rows == []
