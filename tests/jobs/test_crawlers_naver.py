"""네이버 fundamentals/ROE/sector 크롤러 스모크 — respx 로 HTML 고정.

v2 `crawl_naver_fundamentals`/`crawl_naver_roe` 셀렉터 규칙이 유지되는지만
간단히 확인한다. HTML 샘플은 실제 네이버 금융 페이지의 최소 복제본.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.crawlers.naver import (
    crawl_naver_fundamentals,
    crawl_naver_roe,
)

_MAIN_URL_RE = r"https://finance\.naver\.com/item/main\.naver.*"


_ROE_HTML = """
<html><body>
<table>
  <tr><th>ROE(%)</th><td>12.34</td><td>15.67</td><td>-</td></tr>
</table>
</body></html>
"""

_FUND_HTML = """
<html><body>
<table>
  <tr>
    <th>2024.03</th>
    <th>2024.06</th>
    <th>2024.09</th>
    <th>2024.12(E)</th>
  </tr>
  <tr><th>EPS</th><td>100</td><td>200</td><td>300</td><td>400</td></tr>
  <tr><th>PER(배)</th><td>10</td><td>20</td><td>15.5</td><td>14.0</td></tr>
  <tr><th>BPS</th><td>1000</td><td>1100</td><td>1200</td><td>1300</td></tr>
  <tr><th>PBR(배)</th><td>1.0</td><td>1.1</td><td>1.25</td><td>1.3</td></tr>
  <tr><th>ROE(%)</th><td>5.0</td><td>6.0</td><td>7.5</td><td>8.0</td></tr>
</table>
</body></html>
"""


@pytest.mark.asyncio
async def test_crawl_naver_roe_returns_last_valid():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_MAIN_URL_RE).respond(
            200, text=_ROE_HTML, headers={"content-type": "text/html; charset=euc-kr"}
        )
        async with httpx.AsyncClient() as client:
            roe = await crawl_naver_roe(client, "005930")
    assert roe == 15.67


@pytest.mark.asyncio
async def test_crawl_naver_fundamentals_picks_latest_actual_quarter():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_MAIN_URL_RE).respond(
            200, text=_FUND_HTML, headers={"content-type": "text/html; charset=euc-kr"}
        )
        async with httpx.AsyncClient() as client:
            fund = await crawl_naver_fundamentals(client, "005930")
    assert fund is not None
    assert fund.quarter_name == "2024.09"
    assert fund.per == 15.5
    assert fund.pbr == 1.25
    assert fund.roe == 7.5


@pytest.mark.asyncio
async def test_crawl_naver_fundamentals_returns_none_on_empty():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_MAIN_URL_RE).respond(
            200, text="<html></html>", headers={"content-type": "text/html"}
        )
        async with httpx.AsyncClient() as client:
            fund = await crawl_naver_fundamentals(client, "000000")
    assert fund is None
