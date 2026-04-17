"""FnGuide/Naver 컨센서스 크롤러 스모크."""

from __future__ import annotations

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.crawlers.fnguide import (
    crawl_consensus,
    crawl_fnguide_consensus,
)

_FNGUIDE_URL_RE = r"https://comp\.fnguide\.com/.*"
_NAVER_CONSENSUS_URL_RE = r"https://navercomp\.wisereport\.co\.kr/.*"


_FNGUIDE_HTML = """
<html><body>
<table>
  <tr><th>EPS(원)</th><td>1000</td><td>1200</td></tr>
  <tr><th>PER(배)</th><td>10.5</td><td>12.2</td></tr>
  <tr><th>ROE(%)</th><td>8.0</td><td>9.5</td></tr>
</table>
<table>
  <tr><th>목표주가</th><td>80,000</td></tr>
  <tr><th>투자의견</th><td>2.3</td></tr>
  <tr><th>애널리스트</th><td>15</td></tr>
</table>
</body></html>
"""


@pytest.mark.asyncio
async def test_crawl_fnguide_consensus_parses_forward_metrics():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_FNGUIDE_URL_RE).respond(200, text=_FNGUIDE_HTML)
        async with httpx.AsyncClient() as client:
            data = await crawl_fnguide_consensus(client, "005930")
    assert data is not None
    assert data.forward_eps == 1200.0
    assert data.forward_per == 12.2
    assert data.forward_roe == 9.5
    assert data.target_price == 80000
    assert data.investment_opinion == 2.3
    assert data.analyst_count == 15


@pytest.mark.asyncio
async def test_crawl_consensus_applies_thin_coverage_filter():
    thin_html = _FNGUIDE_HTML.replace("<td>15</td>", "<td>2</td>")
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_FNGUIDE_URL_RE).respond(200, text=thin_html)
        mock.get(url__regex=_NAVER_CONSENSUS_URL_RE).respond(200, text="<html/>")
        async with httpx.AsyncClient() as client:
            data = await crawl_consensus(client, "005930")
    assert data is None


@pytest.mark.asyncio
async def test_crawl_consensus_falls_back_to_naver_on_fnguide_empty():
    empty_fnguide = "<html/>"
    naver_html = """
    <html><body>
    <table>
      <tr><th>EPS</th><td>500</td></tr>
      <tr><th>PER</th><td>8.8</td></tr>
    </table>
    <dl><dt>목표주가 50,000</dt></dl>
    </body></html>
    """
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_FNGUIDE_URL_RE).respond(200, text=empty_fnguide)
        mock.get(url__regex=_NAVER_CONSENSUS_URL_RE).respond(200, text=naver_html)
        async with httpx.AsyncClient() as client:
            data = await crawl_consensus(client, "005930")
    assert data is not None
    assert data.source == "NAVER"
    assert data.forward_per == 8.8
