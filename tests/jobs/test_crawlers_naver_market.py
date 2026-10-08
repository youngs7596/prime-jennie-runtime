"""네이버 시장 크롤러 스모크 — fchart XML / 투자자 동향 JSON 고정."""

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

from .naver_trend_fixture import INVESTOR_TREND_URL_RE, trend_json, trend_row

_FCHART_URL_RE = r"https://fchart\.stock\.naver\.com/sise\.nhn.*"

_FCHART_XML = """
<protocol>
  <chartdata symbol="KOSPI">
    <item data="20260101|3000.00|3050.00|2990.00|3010.50|1234567" />
    <item data="20260102|3010.50|3080.00|3000.00|3070.25|2345678" />
    <item data="bad" />
  </chartdata>
</protocol>
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


_TREND = trend_json(trend_row("20260623"), trend_row("20260622", individual=21506))


@pytest.mark.asyncio
async def test_fetch_investor_flows_parses_target_date():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=INVESTOR_TREND_URL_RE).respond(200, json=_TREND)
        async with httpx.AsyncClient() as client:
            flows = await fetch_investor_flows(client, "kospi", "20260622")
    assert flows is not None
    assert flows.trade_date == date(2026, 6, 22)
    assert flows.retail_net == 21506.0
    assert flows.foreign_net == -42047.0
    assert flows.institutional_net == -44759.0


@pytest.mark.asyncio
async def test_fetch_investor_flows_returns_none_on_missing_date():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=INVESTOR_TREND_URL_RE).respond(200, json=_TREND)
        async with httpx.AsyncClient() as client:
            flows = await fetch_investor_flows(client, "kospi", "20260101")
    assert flows is None


@pytest.mark.asyncio
async def test_fetch_market_investor_breakdown_maps_codes():
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__regex=INVESTOR_TREND_URL_RE).respond(200, json=_TREND)
        async with httpx.AsyncClient() as client:
            rows = await fetch_market_investor_breakdown(client, "20260623")
    assert route.calls[0].request.url.params["marketType"] == "KOSPI"
    assert len(rows) == 2
    r = rows[0]
    assert r.trade_date == date(2026, 6, 23)
    assert r.market == "KOSPI"
    # 6-23 폭락일: 개인 매수 / 외국인·기관 매도 / 연기금도 순매도
    assert r.individual_net == 85910.0
    assert r.foreign_net == -42047.0  # 외국인 + 기타외국인
    assert r.trust_net == -19662.0  # 투신 + 사모
    assert r.pension_net == -2937.0  # 연기금이 기관과 별개로 분리됨
    assert r.financial_inv_net == -20908.0
    assert r.etc_corp_net == 898.0
    # 기관계 = 하위 여섯 항목 합
    assert r.institution_net == -44759.0


@pytest.mark.asyncio
async def test_fetch_market_investor_breakdown_rounds_after_summing_won():
    # 원 단위로 다 더한 뒤 억원으로 반올림한다 — 항목별로 먼저 반올림하면 어긋난다.
    row = {
        "bizdate": "20260915",
        "netAmounts": [
            {"investorGubun": "9000", "diffValue": "-1547940000000"},  # -15479.4 억
            {"investorGubun": "9001", "diffValue": "2140000000"},  # 21.4 억
        ],
    }
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=INVESTOR_TREND_URL_RE).respond(200, json=trend_json(row))
        async with httpx.AsyncClient() as client:
            [r] = await fetch_market_investor_breakdown(client, "20260915")
    assert r.foreign_net == -15458.0


@pytest.mark.asyncio
async def test_fetch_market_investor_breakdown_empty_on_bad_response():
    # 출처가 닫히거나(410) 구조가 바뀌면 빈 리스트로 안전 실패.
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=INVESTOR_TREND_URL_RE).respond(410, text="gone")
        async with httpx.AsyncClient() as client:
            assert await fetch_market_investor_breakdown(client, "20260623") == []
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=INVESTOR_TREND_URL_RE).respond(200, json={"content": []})
        async with httpx.AsyncClient() as client:
            assert await fetch_market_investor_breakdown(client, "20260623") == []
