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


# 실제 FnGuide SVD_Main 구조 모사:
# - "업종 비교" 표는 헤더가 [구분, <회사>, 업종평균, KOSPI(시장평균)] 라 회사 값은
#   첫 데이터 셀이다. 마지막 셀(KOSPI 평균)은 전 종목 동일값이라 읽으면 안 된다.
# - PER 의 forward 추정치는 Financial Highlight 표의 최우측 (E) 컬럼에 있다.
_FNGUIDE_HTML = """
<html><body>
<table>
  <caption>업종 비교</caption>
  <tr><th>구분</th><td>삼성전자</td><td>코스피 전기·전자</td><td>KOSPI</td></tr>
  <tr><th>EPS(원)</th><td>6564</td><td>36121.85</td><td>9060.56</td></tr>
  <tr><th>ROE</th><td>10.85</td><td>14.26</td><td>8.84</td></tr>
</table>
<table>
  <caption>Financial Highlight</caption>
  <tr><th>PER(배)수정주가 / 수정EPS</th><td>13.55</td><td>6.86</td><td>5.81</td></tr>
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
    # EPS·ROE 는 업종비교 표의 회사 컬럼(첫 셀)에서 와야 한다.
    assert data.forward_eps == 6564.0
    assert data.forward_roe == 10.85
    # forward_per 는 Financial Highlight 최우측 (E) 컬럼.
    assert data.forward_per == 5.81
    assert data.target_price == 80000
    assert data.investment_opinion == 2.3
    assert data.analyst_count == 15


@pytest.mark.asyncio
async def test_crawl_fnguide_consensus_ignores_industry_average_columns():
    """회귀: EPS·ROE 가 KOSPI 시장평균(마지막 셀)으로 오염되면 안 된다.

    2026-06-07 이전엔 tds[-1] 을 읽어 전 종목 forward_roe=8.84, forward_eps=9060.56
    로 찍혔다 (업종비교 표의 KOSPI 평균 컬럼).
    """
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_FNGUIDE_URL_RE).respond(200, text=_FNGUIDE_HTML)
        async with httpx.AsyncClient() as client:
            data = await crawl_fnguide_consensus(client, "005930")
    assert data is not None
    assert data.forward_roe != 8.84  # KOSPI 평균
    assert data.forward_roe != 14.26  # 업종 평균
    assert data.forward_eps != 9060.56  # KOSPI 평균
    assert data.forward_eps != 36121.85  # 업종 평균


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
