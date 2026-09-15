"""네이버 fundamentals/ROE/sector 크롤러 스모크 — respx 로 JSON 고정.

2026-09-15 사이트 이전 뒤 모바일 JSON API 를 읽는다. 값 고르는 규칙(추정 분기를
건너뛴 최신 분기 / ROE 행의 마지막 유효값)은 v2 때와 같아야 하므로 그 부분만
확인한다. 응답 샘플은 실제 API 의 최소 복제본.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.crawlers.naver import (
    build_naver_sector_mapping,
    crawl_naver_fundamentals,
    crawl_naver_roe,
)

_FINANCE_URL_RE = r"https://m\.stock\.naver\.com/api/stock/\w+/finance/quarter"
_INDUSTRY_LIST_URL_RE = r"https://m\.stock\.naver\.com/api/stocks/industry\?.*"
_INDUSTRY_DETAIL_URL_RE = r"https://m\.stock\.naver\.com/api/stocks/industry/\d+.*"


def _finance_json(per: dict[str, str], pbr: dict[str, str], roe: dict[str, str]) -> dict:
    """마지막 분기만 추정치(2024.12)인 표를 만든다."""
    return {
        "itemCode": "005930",
        "financePeriodType": "QUARTER",
        "financeInfo": {
            "itemCode": "005930",
            "trTitleList": [
                {"isConsensus": "N", "title": "2024.03.", "key": "202403"},
                {"isConsensus": "N", "title": "2024.06.", "key": "202406"},
                {"isConsensus": "N", "title": "2024.09.", "key": "202409"},
                {"isConsensus": "Y", "title": "2024.12.", "key": "202412"},
            ],
            "rowList": [
                {"title": "매출액", "columns": {"202409": {"value": "790,987"}}},
                {"title": "PER", "columns": {k: {"value": v} for k, v in per.items()}},
                {"title": "PBR", "columns": {k: {"value": v} for k, v in pbr.items()}},
                {"title": "ROE", "columns": {k: {"value": v} for k, v in roe.items()}},
            ],
        },
    }


_FINANCE_SAMPLE = _finance_json(
    per={"202403": "10", "202406": "20", "202409": "15.5", "202412": "14.0"},
    pbr={"202403": "1.0", "202406": "1.1", "202409": "1.25", "202412": "1.3"},
    roe={"202403": "5.0", "202406": "6.0", "202409": "7.5", "202412": "8.0"},
)


@pytest.mark.asyncio
async def test_crawl_naver_roe_returns_last_valid():
    """ROE 는 추정 분기까지 포함해 가장 오른쪽 유효값을 쓴다."""
    sample = _finance_json(
        per={"202409": "15.5"},
        pbr={"202409": "1.25"},
        roe={"202403": "12.34", "202406": "15.67", "202409": "-"},
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_FINANCE_URL_RE).respond(200, json=sample)
        async with httpx.AsyncClient() as client:
            roe = await crawl_naver_roe(client, "005930")
    assert roe == 15.67


@pytest.mark.asyncio
async def test_crawl_naver_fundamentals_picks_latest_actual_quarter():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_FINANCE_URL_RE).respond(200, json=_FINANCE_SAMPLE)
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
        mock.get(url__regex=_FINANCE_URL_RE).respond(200, json={})
        async with httpx.AsyncClient() as client:
            fund = await crawl_naver_fundamentals(client, "000000")
    assert fund is None


@pytest.mark.asyncio
async def test_crawl_naver_fundamentals_returns_none_on_redirect_body():
    """이전된 옛 주소처럼 본문이 JSON 이 아니면 조용히 성공하지 않고 None 을 준다."""
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_FINANCE_URL_RE).respond(200, text="<!DOCTYPE html>")
        async with httpx.AsyncClient() as client:
            fund = await crawl_naver_fundamentals(client, "005930")
    assert fund is None


@pytest.mark.asyncio
async def test_build_naver_sector_mapping_pages_each_industry():
    """업종 목록 → 업종별 종목 두 단계를 거쳐 종목코드마다 업종명이 붙는다."""
    groups = {
        "groups": [
            {"no": 278, "name": "반도체와반도체장비", "totalCount": 2},
            {"no": 304, "name": "은행", "totalCount": 1},
        ],
        "totalCount": 2,
    }
    detail = {
        "278": {"stocks": [{"itemCode": "005930"}, {"itemCode": "000660"}], "totalCount": 2},
        "304": {"stocks": [{"itemCode": "105560"}], "totalCount": 1},
    }

    def _detail_response(request: httpx.Request) -> httpx.Response:
        sector_no = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=detail[sector_no])

    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_INDUSTRY_DETAIL_URL_RE).mock(side_effect=_detail_response)
        mock.get(url__regex=_INDUSTRY_LIST_URL_RE).respond(200, json=groups)
        async with httpx.AsyncClient() as client:
            mapping = await build_naver_sector_mapping(client, request_delay=0.0)

    assert mapping == {
        "005930": "반도체와반도체장비",
        "000660": "반도체와반도체장비",
        "105560": "은행",
    }
