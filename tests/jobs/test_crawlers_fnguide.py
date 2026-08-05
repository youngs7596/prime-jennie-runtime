"""FnGuide/Naver 컨센서스 크롤러 스모크.

2026-08-05 에 FnGuide 가 기업정보를 새 주소로 옮기면서 표 구조도 바뀌었다. 이 파일은
새 구조(Snapshot 페이지 + 업종비교 JSON)를 모사한다. 옛 구조를 모사하던 판은
`.ai/sessions/session-2026-08-05-*` 시점에 통째로 교체됐다.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.crawlers.fnguide import (
    crawl_consensus,
    crawl_fnguide_consensus,
)

_SNAPSHOT_URL_RE = r"https://wcomp\.fnguide\.com/CompanyInfo/Snapshot.*"
_SECTOR_ROE_URL_RE = r"https://wcomp\.fnguide\.com/CompanyInfo/getSnpSectorChart.*"
_NAVER_CONSENSUS_URL_RE = r"https://navercomp\.wisereport\.co\.kr/.*"


def _snapshot_html(name: str, code: str, *, covered: bool = True) -> str:
    """새 Snapshot 페이지 모사.

    - 제목이 "<회사명>(<종목코드>)" 로 시작한다 — 종목 대조의 근거.
    - '투자의견' 표가 열 방향으로 [투자의견, 목표주가, EPS, PER, 추정기관수] 를 준다.
    - 커버 없는 종목은 데이터 자리에 "관련 데이터가 없습니다" 한 칸만 찍힌다.
    """
    body = (
        "<tr><td>4.0</td><td>416,800</td><td>43,388</td><td>6.8</td><td>25</td></tr>"
        if covered
        else "<tr><td>관련 데이터가 없습니다.</td></tr>"
    )
    return f"""
<html><head><title>{name}({code}) | Snapshot | 기업정보 | Company Guide</title></head>
<body>
<table>
  <caption>투자의견</caption>
  <thead>
    <tr><th>투자의견</th><th>목표주가</th><th>EPS</th><th>PER</th><th>추정기관수</th></tr>
  </thead>
  <tbody>{body}</tbody>
</table>
</body></html>
"""


# 업종비교 위젯 JSON: [회사, 업종, 코스피 업종, 코스피] 순서이고 '26E 열이 추정치다.
_SECTOR_ROE_JSON = {
    "dataset": {
        "header": [
            {"ID": "CMP_NM", "NM": "", "DIGIT": -1},
            {"ID": "VAL1", "NM": "'24", "DIGIT": 2},
            {"ID": "VAL2", "NM": "'25", "DIGIT": 2},
            {"ID": "VAL3", "NM": "'26E", "DIGIT": 2},
        ],
        "data": [
            {"CMP_NM": "삼성전자", "VAL1": 9.03, "VAL2": 10.85, "VAL3": 55.9},
            {"CMP_NM": "반도체", "VAL1": 11.82, "VAL2": 16.53, "VAL3": 69.28},
            {"CMP_NM": "코스피 전기·전자", "VAL1": 9.81, "VAL2": 14.28, "VAL3": 60.86},
            {"CMP_NM": "코스피", "VAL1": 7.48, "VAL2": 8.84, "VAL3": 28.52},
        ],
    }
}


@pytest.mark.asyncio
async def test_crawl_fnguide_consensus_parses_forward_metrics():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_SNAPSHOT_URL_RE).respond(
            200, text=_snapshot_html("삼성전자", "005930")
        )
        mock.get(url__regex=_SECTOR_ROE_URL_RE).respond(200, json=_SECTOR_ROE_JSON)
        async with httpx.AsyncClient() as client:
            data = await crawl_fnguide_consensus(client, "005930")
    assert data is not None
    assert data.forward_eps == 43388.0
    assert data.forward_per == 6.8
    assert data.target_price == 416800
    assert data.investment_opinion == 4.0
    assert data.analyst_count == 25
    # forward ROE 는 업종비교 JSON 의 회사 줄 × 추정('26E) 열.
    assert data.forward_roe == 55.9


@pytest.mark.asyncio
async def test_forward_roe_ignores_sector_and_market_rows():
    """회귀: ROE 가 업종·시장 평균으로 오염되면 안 된다.

    2026-06-07 이전엔 업종비교 표의 마지막 셀(KOSPI 평균)을 읽어 전 종목이 같은 값으로
    찍혔다. JSON 으로 바뀐 지금도 회사는 첫 줄이고 뒤 세 줄은 평균이다.
    """
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_SNAPSHOT_URL_RE).respond(
            200, text=_snapshot_html("삼성전자", "005930")
        )
        mock.get(url__regex=_SECTOR_ROE_URL_RE).respond(200, json=_SECTOR_ROE_JSON)
        async with httpx.AsyncClient() as client:
            data = await crawl_fnguide_consensus(client, "005930")
    assert data is not None
    assert data.forward_roe != 28.52  # 코스피
    assert data.forward_roe != 60.86  # 코스피 업종
    assert data.forward_roe != 69.28  # 업종


@pytest.mark.asyncio
async def test_rejects_page_for_a_different_stock():
    """회귀: 남의 종목 페이지가 오면 버린다 (2026-08-05 사고).

    사이트 이전 직후 옛 주소가 종목 파라미터를 잃어버려 어느 종목을 넣든 기본 페이지인
    삼성전자가 왔다. 파싱은 멀쩡히 성공했기 때문에 8거래일 동안 213 종목이 삼성전자
    숫자를 받아 적었다. 조회한 코드와 페이지가 밝힌 코드가 다르면 즉시 버려야 한다.
    """
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_SNAPSHOT_URL_RE).respond(
            200, text=_snapshot_html("삼성전자", "005930")
        )
        mock.get(url__regex=_SECTOR_ROE_URL_RE).respond(200, json=_SECTOR_ROE_JSON)
        async with httpx.AsyncClient() as client:
            data = await crawl_fnguide_consensus(client, "000660")
    assert data is None


@pytest.mark.asyncio
async def test_rejects_common_share_served_for_preferred_code():
    """우선주 코드를 넣으면 보통주 페이지가 온다 — 같은 대조로 걸러진다.

    A001527(동양3우B) 을 조회하면 동양(001520) 이 온다. 우선주는 컨센서스가 따로 없으니
    보통주 숫자를 우선주 행에 적느니 비우는 쪽이 맞다.
    """
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_SNAPSHOT_URL_RE).respond(200, text=_snapshot_html("동양", "001520"))
        mock.get(url__regex=_SECTOR_ROE_URL_RE).respond(200, json=_SECTOR_ROE_JSON)
        async with httpx.AsyncClient() as client:
            data = await crawl_fnguide_consensus(client, "001527")
    assert data is None


@pytest.mark.asyncio
async def test_rejects_error_page_without_stock_code():
    """옛 주소가 지금 주는 "페이지가 없습니다" 안내문은 200 으로 온다.

    제목에 종목코드가 없으므로 대조에 걸려 버려진다.
    """
    error_page = (
        "<html><head><title>FnGuide,Your Best Financial Guide</title></head>"
        "<body>페이지가 없습니다.</body></html>"
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_SNAPSHOT_URL_RE).respond(200, text=error_page)
        async with httpx.AsyncClient() as client:
            data = await crawl_fnguide_consensus(client, "005930")
    assert data is None


@pytest.mark.asyncio
async def test_uncovered_stock_yields_nothing_from_fnguide():
    """추정 표가 비면 FnGuide 에서 건질 게 없다 — 업종비교 표는 이제 서버가 안 그려 준다."""
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_SNAPSHOT_URL_RE).respond(
            200, text=_snapshot_html("중소형주", "001500", covered=False)
        )
        async with httpx.AsyncClient() as client:
            data = await crawl_fnguide_consensus(client, "001500")
    assert data is None


@pytest.mark.asyncio
async def test_keeps_record_when_roe_lookup_fails():
    """ROE 조회가 실패해도 컨센서스 레코드는 살린다 — 목표주가가 본진이다."""
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_SNAPSHOT_URL_RE).respond(
            200, text=_snapshot_html("삼성전자", "005930")
        )
        mock.get(url__regex=_SECTOR_ROE_URL_RE).respond(500)
        async with httpx.AsyncClient() as client:
            data = await crawl_fnguide_consensus(client, "005930")
    assert data is not None
    assert data.target_price == 416800
    assert data.forward_roe is None


@pytest.mark.asyncio
async def test_crawl_consensus_keeps_thin_coverage_record():
    """추정기관수가 적어도 레코드를 통째로 버리지 않는다 (2026-06-09 결정)."""
    thin = _snapshot_html("삼성전자", "005930").replace("<td>25</td>", "<td>2</td>")
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_SNAPSHOT_URL_RE).respond(200, text=thin)
        mock.get(url__regex=_SECTOR_ROE_URL_RE).respond(200, json=_SECTOR_ROE_JSON)
        mock.get(url__regex=_NAVER_CONSENSUS_URL_RE).respond(200, text="<html/>")
        async with httpx.AsyncClient() as client:
            data = await crawl_consensus(client, "005930")
    assert data is not None
    assert data.analyst_count == 2
    assert data.source == "FNGUIDE"


@pytest.mark.asyncio
async def test_crawl_consensus_falls_back_to_naver_on_fnguide_empty():
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
        mock.get(url__regex=_SNAPSHOT_URL_RE).respond(200, text="<html/>")
        mock.get(url__regex=_NAVER_CONSENSUS_URL_RE).respond(200, text=naver_html)
        async with httpx.AsyncClient() as client:
            data = await crawl_consensus(client, "005930")
    assert data is not None
    assert data.source == "NAVER"
    assert data.forward_per == 8.8
