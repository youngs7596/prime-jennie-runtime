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

from .naver_trend_fixture import INVESTOR_TREND_URL_RE, trend_json, trend_row

_MAIN_URL_RE = r"https://m\.stock\.naver\.com/api/stock/\w+/finance/quarter"
_SECTOR_LIST_URL = r"https://m\.stock\.naver\.com/api/stocks/industry\?.*"
_SECTOR_DETAIL_URL = r"https://m\.stock\.naver\.com/api/stocks/industry/\d+.*"
_FNGUIDE_URL = r"https://wcomp\.fnguide\.com/CompanyInfo/Snapshot.*"
_FNGUIDE_ROE_URL = r"https://wcomp\.fnguide\.com/CompanyInfo/getSnpSectorChart.*"
_NAVER_CONSENSUS_URL = r"https://navercomp\.wisereport\.co\.kr/.*"

# 분기 재무표 — 최신 실적 2024.09: PER=12.5, PBR=1.2, ROE=8.0. 마지막 2024.12 는 추정치라
# fundamentals 는 건너뛰고, ROE 단독 크롤은 그 추정값(9.0)까지 본다 — 둘이 다른 값을 봐야
# roe 교차검증이 의미가 있다.
_MAIN_JSON = {
    "itemCode": "005930",
    "financeInfo": {
        "trTitleList": [
            {"isConsensus": "N", "title": "2024.03.", "key": "202403"},
            {"isConsensus": "N", "title": "2024.06.", "key": "202406"},
            {"isConsensus": "N", "title": "2024.09.", "key": "202409"},
            {"isConsensus": "Y", "title": "2024.12.", "key": "202412"},
        ],
        "rowList": [
            {
                "title": "PER",
                "columns": {
                    "202403": {"value": "10"},
                    "202406": {"value": "11"},
                    "202409": {"value": "12.5"},
                    "202412": {"value": "14.0"},
                },
            },
            {
                "title": "PBR",
                "columns": {
                    "202403": {"value": "1.0"},
                    "202406": {"value": "1.1"},
                    "202409": {"value": "1.2"},
                    "202412": {"value": "1.3"},
                },
            },
            {
                "title": "ROE",
                "columns": {
                    "202403": {"value": "6.0"},
                    "202406": {"value": "7.0"},
                    "202409": {"value": "8.0"},
                    "202412": {"value": "9.0"},
                },
            },
        ],
    },
}

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


def _sector_list_json() -> dict:
    # 업종 2개
    return {
        "groups": [
            {"no": 1, "name": "반도체", "totalCount": 551},
            {"no": 2, "name": "자동차", "totalCount": 551},
        ],
        "totalCount": 2,
    }


def _sector_detail_json(include_sentinel: bool, extra_count: int) -> dict:
    codes = [CONTRACT_SMOKE_SENTINEL] if include_sentinel else []
    codes += [f"{100000 + i:06d}" for i in range(extra_count)]
    return {
        "stocks": [{"itemCode": c} for c in codes],
        "totalCount": len(codes),
    }


def _investor_json(bizdate: str, **eok: float) -> dict:
    """시장 전체 수급 응답. 기본값은 2026-06-23 실측치 — 개인+외국인+기관+기타법인 ≈ 0."""
    return trend_json(trend_row(bizdate, **eok))


@dataclass
class _FakeArticle:
    title: str
    ticker: str


@dataclass
class _FakeNewsCrawler:
    articles: list[_FakeArticle]

    async def crawl(self, universe: list[str]) -> list[_FakeArticle]:
        return [a for a in self.articles if a.ticker in universe]


def _mock_all(mock, investor_json: dict) -> None:
    """수급 페이지만 갈아 끼우고 나머지 다섯 크롤러는 정상 응답으로 고정."""
    mock.get(url__regex=_MAIN_URL_RE).respond(200, json=_MAIN_JSON)
    # 업종 상세를 목록보다 먼저 걸어야 경로가 안 겹친다. 두 업종 모두 sentinel + 550개라
    # 합산 dict 크기 551 → 최소 500종목 조건을 넘긴다.
    mock.get(url__regex=_SECTOR_DETAIL_URL).respond(200, json=_sector_detail_json(True, 550))
    mock.get(url__regex=_SECTOR_LIST_URL).respond(200, json=_sector_list_json())
    mock.get(url__regex=_FNGUIDE_URL).respond(200, text=_FNGUIDE_HTML)
    mock.get(url__regex=_FNGUIDE_ROE_URL).respond(200, json=_FNGUIDE_ROE_JSON)
    mock.get(url__regex=INVESTOR_TREND_URL_RE).respond(200, json=investor_json)


@pytest.mark.asyncio
async def test_contract_smoke_passes_when_all_contracts_intact():
    today_bizdate = date.today().strftime("%Y%m%d")
    with respx.mock(assert_all_called=False) as mock:
        _mock_all(mock, _investor_json(today_bizdate))
        async with httpx.AsyncClient() as client:
            news = _FakeNewsCrawler(
                articles=[_FakeArticle(title="반도체 특허", ticker=CONTRACT_SMOKE_SENTINEL)]
            )
            await contract_smoke_test(client, news)


@pytest.mark.asyncio
async def test_contract_smoke_raises_when_news_empty():
    today_bizdate = date.today().strftime("%Y%m%d")
    with respx.mock(assert_all_called=False) as mock:
        _mock_all(mock, _investor_json(today_bizdate))
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
    investor = _investor_json(
        today_bizdate,
        individual=-11652,
        foreign=-1760,
        financial_inv=1119,
        insurance=-109,
        trust=840,
        bank=-54,
        etc_finance=221,
        pension=464,
        etc_corp=10931,
    )
    with respx.mock(assert_all_called=False) as mock:
        _mock_all(mock, investor)
        async with httpx.AsyncClient() as client:
            news = _FakeNewsCrawler(
                articles=[_FakeArticle(title="반도체 특허", ticker=CONTRACT_SMOKE_SENTINEL)]
            )
            await contract_smoke_test(client, news)


@pytest.mark.asyncio
async def test_contract_smoke_raises_when_buy_sell_identity_breaks():
    """매수·매도 합이 안 맞으면 실패 — 컬럼 매핑이 밀리는 사고를 잡는 검사."""
    today_bizdate = date.today().strftime("%Y%m%d")
    investor = _investor_json(today_bizdate, etc_corp=0)
    with respx.mock(assert_all_called=False) as mock:
        _mock_all(mock, investor)
        async with httpx.AsyncClient() as client:
            news = _FakeNewsCrawler(
                articles=[_FakeArticle(title="반도체 특허", ticker=CONTRACT_SMOKE_SENTINEL)]
            )
            with pytest.raises(ContractSmokeError, match="매수·매도 합이 안 맞음"):
                await contract_smoke_test(client, news)
