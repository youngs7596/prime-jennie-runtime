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
_FNGUIDE_URL = r"https://comp\.fnguide\.com/.*"
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

_FNGUIDE_HTML = """
<html><body>
<table>
  <tr><th>EPS(원)</th><td>1200</td></tr>
  <tr><th>PER(배)</th><td>11.5</td></tr>
  <tr><th>ROE(%)</th><td>8.5</td></tr>
</table>
<table>
  <tr><th>애널리스트</th><td>15</td></tr>
</table>
</body></html>
"""


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


def _investor_html(bizdate: str) -> str:
    short = bizdate[2:4] + "." + bizdate[4:6] + "." + bizdate[6:8]
    return f"""
    <html><body>
    <table class="type_1">
      <tr><th>날짜</th><th>외국인(억)</th><th>기관계</th><th>개인</th></tr>
      <tr><td>{short}</td><td>1,000</td><td>-500</td><td>-500</td></tr>
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


@pytest.mark.asyncio
async def test_contract_smoke_passes_when_all_contracts_intact():
    today_bizdate = date.today().strftime("%Y%m%d")
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_MAIN_URL_RE).respond(200, content=_MAIN_HTML.encode("euc-kr"))
        mock.get(url__regex=_SECTOR_LIST_URL).respond(
            200, content=_sector_list_html().encode("euc-kr")
        )
        # sector detail — 두 섹터 모두 sentinel + 550개. 합산 dict 크기 551 → 500 넘김.
        mock.get(url__regex=_SECTOR_DETAIL_URL).respond(
            200, content=_sector_detail_html(True, 550).encode("euc-kr")
        )
        mock.get(url__regex=_FNGUIDE_URL).respond(200, text=_FNGUIDE_HTML)
        mock.get(url__regex=_INVESTOR_URL).respond(
            200, content=_investor_html(today_bizdate).encode("euc-kr")
        )
        async with httpx.AsyncClient() as client:
            news = _FakeNewsCrawler(
                articles=[_FakeArticle(title="반도체 특허", ticker=CONTRACT_SMOKE_SENTINEL)]
            )
            await contract_smoke_test(client, news)


@pytest.mark.asyncio
async def test_contract_smoke_raises_when_news_empty():
    today_bizdate = date.today().strftime("%Y%m%d")
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_MAIN_URL_RE).respond(200, content=_MAIN_HTML.encode("euc-kr"))
        mock.get(url__regex=_SECTOR_LIST_URL).respond(
            200, content=_sector_list_html().encode("euc-kr")
        )
        mock.get(url__regex=_SECTOR_DETAIL_URL).respond(
            200, content=_sector_detail_html(True, 550).encode("euc-kr")
        )
        mock.get(url__regex=_FNGUIDE_URL).respond(200, text=_FNGUIDE_HTML)
        mock.get(url__regex=_INVESTOR_URL).respond(
            200, content=_investor_html(today_bizdate).encode("euc-kr")
        )
        async with httpx.AsyncClient() as client:
            news = _FakeNewsCrawler(articles=[])
            with pytest.raises(ContractSmokeError, match="news: no articles"):
                await contract_smoke_test(client, news)
