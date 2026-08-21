"""`contract_smoke_test` 스모크 — respx 로 모든 외부 응답 고정.

실 네트워크 없이 6개 crawler 검증 경로가 모두 통과하고, 하나라도 깨지면
`ContractSmokeError` 가 뜨는지 확인한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.maintenance import (
    CONTRACT_SMOKE_SENTINEL,
    ContractSmokeError,
    contract_smoke_test,
)

_MAIN_URL_RE = r"https://finance\.naver\.com/item/main\.naver.*"
_SECTOR_LIST_URL = r"https://finance\.naver\.com/sise/sise_group\.naver.*"
_SECTOR_DETAIL_URL = r"https://finance\.naver\.com/sise/sise_group_detail\.naver.*"
_FNGUIDE_URL = r"https://wcomp\.fnguide\.com/CompanyInfo/Snapshot.*"
_FNGUIDE_ROE_URL = r"https://wcomp\.fnguide\.com/CompanyInfo/getSnpSectorChart.*"
_NAVER_CONSENSUS_URL = r"https://navercomp\.wisereport\.co\.kr/.*"
_INVESTOR_URL = r"https://finance\.naver\.com/sise/investorDealTrendDay\.naver.*"

# 주요재무정보 테이블 — 최신 실적 2024.09: PER=12.5, PBR=1.2, ROE=8.0
_MAIN_HTML = """
<html><body>
<table>
  <tr><th>ROE(%)</th><td>7.0</td><td>8.0</td><td>-</td></tr>
</table>
<table>
  <tr><th>2024.03</th><th>2024.06</th><th>2024.09</th><th>2024.12(E)</th></tr>
  <tr><th>EPS</th><td>100</td><td>200</td><td>300</td><td>400</td></tr>
  <tr><th>PER(배)</th><td>10</td><td>11</td><td>12.5</td><td>14.0</td></tr>
  <tr><th>BPS</th><td>1000</td><td>1100</td><td>1200</td><td>1300</td></tr>
  <tr><th>PBR(배)</th><td>1.0</td><td>1.1</td><td>1.2</td><td>1.3</td></tr>
  <tr><th>ROE(%)</th><td>6.0</td><td>7.0</td><td>8.0</td><td>9.0</td></tr>
</table>
</body></html>
"""

# 2026-08-05 이후의 FnGuide Snapshot 구조. 제목이 종목을 밝히고(크롤러가 이걸로 대조한다)
# '투자의견' 표가 열 방향으로 값을 준다. sentinel 은 삼성전자(005930).
_FNGUIDE_HTML = """
<html><head><title>삼성전자(005930) | Snapshot | 기업정보 | Company Guide</title></head>
<body>
<table>
  <caption>투자의견</caption>
  <thead>
    <tr><th>투자의견</th><th>목표주가</th><th>EPS</th><th>PER</th><th>추정기관수</th></tr>
  </thead>
  <tbody>
    <tr><td>4.0</td><td>416,800</td><td>1,200</td><td>11.5</td><td>15</td></tr>
  </tbody>
</table>
</body></html>
"""

# 업종비교 위젯 JSON — 첫 줄이 회사, 마지막 열('26E)이 추정치.
_FNGUIDE_ROE_JSON = {
    "dataset": {
        "header": [
            {"ID": "CMP_NM", "NM": "", "DIGIT": -1},
            {"ID": "VAL1", "NM": "'25", "DIGIT": 2},
            {"ID": "VAL2", "NM": "'26E", "DIGIT": 2},
        ],
        "data": [
            {"CMP_NM": "삼성전자", "VAL1": 8.0, "VAL2": 8.5},
            {"CMP_NM": "코스피", "VAL1": 7.0, "VAL2": 7.5},
        ],
    }
}


def _sector_list_html() -> str:
    # 업종 2개
    return """
    <html><body>
    <table class="type_1">
      <tr><td><a href="/sise/sise_group_detail.naver?type=upjong&no=001">반도체</a></td></tr>
      <tr><td><a href="/sise/sise_group_detail.naver?type=upjong&no=002">자동차</a></td></tr>
    </table>
    </body></html>
    """


def _sector_detail_html(include_sentinel: bool, extra_count: int) -> str:
    rows = []
    if include_sentinel:
        rows.append(
            f'<tr><td><a href="/item/main.naver?code={CONTRACT_SMOKE_SENTINEL}">s</a></td></tr>'
        )
    for i in range(extra_count):
        code = f"{100000 + i:06d}"
        rows.append(f'<tr><td><a href="/item/main.naver?code={code}">x</a></td></tr>')
    return '<html><body><table class="type_5">' + "".join(rows) + "</table></body></html>"


# 실제 investorDealTrendDay 구조: 2단 헤더(기관 colspan=6 그룹) + 기타법인 컬럼.
# 기본값은 2026-06-23 실측치 — 개인+외국인+기관계+기타법인 ≈ 0 이고 기관 하위 여섯
# 항목 합이 기관계와 같다.
def _investor_html(
    bizdate: str,
    *,
    individual: str = "85,910",
    foreign: str = "-42,047",
    institution: str = "-44,760",
    financial_inv: str = "-20,908",
    insurance: str = "-1,018",
    trust: str = "-19,662",
    bank: str = "-165",
    etc_finance: str = "-69",
    pension: str = "-2,937",
    etc_corp: str = "898",
) -> str:
    short = bizdate[2:4] + "." + bizdate[4:6] + "." + bizdate[6:8]
    return f"""
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
        <td>{short}</td><td>{individual}</td><td>{foreign}</td><td>{institution}</td>
        <td>{financial_inv}</td><td>{insurance}</td><td>{trust}</td><td>{bank}</td>
        <td>{etc_finance}</td><td>{pension}</td><td>{etc_corp}</td>
      </tr>
    </table>
    </body></html>
    """


@dataclass
class _FakeArticle:
    title: str
    ticker: str


@dataclass
class _FakeNewsCrawler:
    articles: list[_FakeArticle]

    async def crawl(self, universe: list[str]) -> list[_FakeArticle]:
        return [a for a in self.articles if a.ticker in universe]


def _mock_all(mock, investor_html: str) -> None:
    """수급 페이지만 갈아 끼우고 나머지 다섯 크롤러는 정상 응답으로 고정."""
    mock.get(url__regex=_MAIN_URL_RE).respond(200, content=_MAIN_HTML.encode("euc-kr"))
    mock.get(url__regex=_SECTOR_LIST_URL).respond(200, content=_sector_list_html().encode("euc-kr"))
    # sector detail — 두 섹터 모두 sentinel + 550개. 합산 dict 크기 551 → 500 넘김.
    mock.get(url__regex=_SECTOR_DETAIL_URL).respond(
        200, content=_sector_detail_html(True, 550).encode("euc-kr")
    )
    mock.get(url__regex=_FNGUIDE_URL).respond(200, text=_FNGUIDE_HTML)
    mock.get(url__regex=_FNGUIDE_ROE_URL).respond(200, json=_FNGUIDE_ROE_JSON)
    mock.get(url__regex=_INVESTOR_URL).respond(200, content=investor_html.encode("euc-kr"))


@pytest.mark.asyncio
async def test_contract_smoke_passes_when_all_contracts_intact():
    today_bizdate = date.today().strftime("%Y%m%d")
    with respx.mock(assert_all_called=False) as mock:
        _mock_all(mock, _investor_html(today_bizdate))
        async with httpx.AsyncClient() as client:
            news = _FakeNewsCrawler(
                articles=[_FakeArticle(title="반도체 특허", ticker=CONTRACT_SMOKE_SENTINEL)]
            )
            await contract_smoke_test(client, news)


@pytest.mark.asyncio
async def test_contract_smoke_raises_when_news_empty():
    today_bizdate = date.today().strftime("%Y%m%d")
    with respx.mock(assert_all_called=False) as mock:
        _mock_all(mock, _investor_html(today_bizdate))
        async with httpx.AsyncClient() as client:
            news = _FakeNewsCrawler(articles=[])
            with pytest.raises(ContractSmokeError, match="news: no articles"):
                await contract_smoke_test(client, news)


@pytest.mark.asyncio
async def test_contract_smoke_passes_when_etc_corp_is_huge():
    """기타법인이 1조를 넘어도 통과해야 한다 — 2026-08-20·21 오탐 회귀.

    옛 검사는 외국인+기관계+개인 셋만 더해 1조 안쪽인지 봤다. 아래 값은 8-21 실측치로,
    기타법인 1.09조가 빠지면 잔차가 −1.09조가 되어 멀쩡한 데이터가 실패로 신고된다.
    """
    today_bizdate = date.today().strftime("%Y%m%d")
    html = _investor_html(
        today_bizdate,
        individual="-11,652",
        foreign="-1,760",
        institution="2,481",
        financial_inv="1,119",
        insurance="-109",
        trust="840",
        bank="-54",
        etc_finance="221",
        pension="464",
        etc_corp="10,931",
    )
    with respx.mock(assert_all_called=False) as mock:
        _mock_all(mock, html)
        async with httpx.AsyncClient() as client:
            news = _FakeNewsCrawler(
                articles=[_FakeArticle(title="반도체 특허", ticker=CONTRACT_SMOKE_SENTINEL)]
            )
            await contract_smoke_test(client, news)


@pytest.mark.asyncio
async def test_contract_smoke_raises_when_buy_sell_identity_breaks():
    """매수·매도 합이 안 맞으면 실패 — 컬럼 매핑이 밀리는 사고를 잡는 검사."""
    today_bizdate = date.today().strftime("%Y%m%d")
    html = _investor_html(today_bizdate, etc_corp="0")
    with respx.mock(assert_all_called=False) as mock:
        _mock_all(mock, html)
        async with httpx.AsyncClient() as client:
            news = _FakeNewsCrawler(
                articles=[_FakeArticle(title="반도체 특허", ticker=CONTRACT_SMOKE_SENTINEL)]
            )
            with pytest.raises(ContractSmokeError, match="매수·매도 합이 안 맞음"):
                await contract_smoke_test(client, news)


@pytest.mark.asyncio
async def test_contract_smoke_raises_when_institution_parts_mismatch():
    """기관 하위 항목 합이 기관계와 다르면 실패."""
    today_bizdate = date.today().strftime("%Y%m%d")
    # 연기금만 0 으로 — 기관계는 그대로라 바깥 항등식은 유지되고 하위 합만 어긋난다.
    html = _investor_html(today_bizdate, pension="0")
    with respx.mock(assert_all_called=False) as mock:
        _mock_all(mock, html)
        async with httpx.AsyncClient() as client:
            news = _FakeNewsCrawler(
                articles=[_FakeArticle(title="반도체 특허", ticker=CONTRACT_SMOKE_SENTINEL)]
            )
            with pytest.raises(ContractSmokeError, match="기관 하위 합이 기관계와 다름"):
                await contract_smoke_test(client, news)
