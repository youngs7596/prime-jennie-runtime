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
#   여기서 forward ROE 를 읽는다 (추정 표엔 ROE 가 없다).
# - "투자의견 컨센서스" 추정 표는 열 방향이다. 헤더 한 줄에 [투자의견, 목표주가, EPS,
#   PER, 추정기관수] 가 있고 값은 그 아래 데이터 한 줄. 목표주가·추정기관수·투자의견과
#   진짜 forward EPS·PER 이 여기 있다.
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
<table class="us_table_ty1">
  <thead>
    <tr class="th_b">
      <th>투자의견</th><th>목표주가</th><th>EPS</th><th>PER</th><th>추정기관수</th>
    </tr>
  </thead>
  <tbody>
    <tr><td>4.0</td><td>416,800</td><td>43,388</td><td>6.8</td><td>25</td></tr>
  </tbody>
</table>
</body></html>
"""

# 커버 없는 종목: 추정 표 자리에 "관련 데이터가 없습니다" 한 칸만 찍힌다.
_FNGUIDE_HTML_NO_COVERAGE = """
<html><body>
<table>
  <caption>업종 비교</caption>
  <tr><th>구분</th><td>중소형주</td><td>코스피 기타</td><td>KOSPI</td></tr>
  <tr><th>EPS(원)</th><td>1200</td><td>36121.85</td><td>9060.56</td></tr>
  <tr><th>ROE</th><td>7.5</td><td>14.26</td><td>8.84</td></tr>
</table>
<table class="us_table_ty1">
  <thead>
    <tr class="th_b">
      <th>투자의견</th><th>목표주가</th><th>EPS</th><th>PER</th><th>추정기관수</th>
    </tr>
  </thead>
  <tbody>
    <tr><td>관련 데이터가 없습니다.</td></tr>
  </tbody>
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
    # forward EPS·PER 은 "투자의견 컨센서스" 추정 표(증권사 평균 추정치).
    assert data.forward_eps == 43388.0
    assert data.forward_per == 6.8
    # forward ROE 는 업종비교 표 회사 컬럼(첫 셀) — 추정 표엔 ROE 가 없다.
    assert data.forward_roe == 10.85
    # 목표주가·투자의견·추정기관수도 추정 표에서.
    assert data.target_price == 416800
    assert data.investment_opinion == 4.0
    assert data.analyst_count == 25


@pytest.mark.asyncio
async def test_crawl_fnguide_consensus_ignores_industry_average_columns():
    """회귀: ROE 가 KOSPI 시장평균(마지막 셀)으로 오염되면 안 된다.

    2026-06-07 이전엔 tds[-1] 을 읽어 전 종목 forward_roe=8.84 로 찍혔다
    (업종비교 표의 KOSPI 평균 컬럼).
    """
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_FNGUIDE_URL_RE).respond(200, text=_FNGUIDE_HTML)
        async with httpx.AsyncClient() as client:
            data = await crawl_fnguide_consensus(client, "005930")
    assert data is not None
    assert data.forward_roe != 8.84  # KOSPI 평균
    assert data.forward_roe != 14.26  # 업종 평균


@pytest.mark.asyncio
async def test_crawl_fnguide_consensus_falls_back_to_trailing_when_uncovered():
    """커버 없는 종목: 추정 표가 빈 칸이라 forward EPS 는 업종비교 trailing 으로 폴백,
    목표주가·추정기관수는 None 이어야 한다."""
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_FNGUIDE_URL_RE).respond(200, text=_FNGUIDE_HTML_NO_COVERAGE)
        async with httpx.AsyncClient() as client:
            data = await crawl_fnguide_consensus(client, "001500")
    assert data is not None
    assert data.forward_eps == 1200.0  # 업종비교 회사 컬럼 폴백
    assert data.forward_roe == 7.5
    assert data.target_price is None
    assert data.analyst_count is None


@pytest.mark.asyncio
async def test_crawl_consensus_keeps_thin_coverage_record():
    """추정기관수가 적어도 레코드를 통째로 버리지 않는다 (2026-06-09 결정).

    얇은 커버여도 업종비교에서 온 trailing EPS·ROE 는 쓸모가 있어 보존한다.
    """
    thin_html = _FNGUIDE_HTML.replace("<td>25</td>", "<td>2</td>")
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_FNGUIDE_URL_RE).respond(200, text=thin_html)
        mock.get(url__regex=_NAVER_CONSENSUS_URL_RE).respond(200, text="<html/>")
        async with httpx.AsyncClient() as client:
            data = await crawl_consensus(client, "005930")
    assert data is not None
    assert data.analyst_count == 2
    assert data.forward_roe == 10.85


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
