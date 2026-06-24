"""CNBC VKOSPI 크롤러 스모크 — priceBars JSON 고정."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.crawlers.cnbc_market import fetch_vkospi_daily

_CNBC_URL_RE = r"https://ts-api\.cnbc\.com/harmony/app/charts/.*"

_CNBC_JSON = {
    "barData": {
        "symbol": ".KSVKOSPI",
        "priceBars": [
            {
                "open": "24.4200",
                "high": "25.8600",
                "low": "24.3700",
                "close": "25.7600",
                "volume": 0,
                "tradeTime": "20260101000000",
            },
            # 정렬 검증용으로 일부러 역순 배치
            {
                "open": "87.9900",
                "high": "89.6900",
                "low": "85.9000",
                "close": "89.4100",
                "volume": 0,
                "tradeTime": "20260623000000",
            },
            {"open": "x", "close": "bad", "tradeTime": "20260102000000"},  # 깨진 봉 → skip
        ],
    }
}


@pytest.mark.asyncio
async def test_fetch_vkospi_daily_parses_and_sorts():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_CNBC_URL_RE).respond(200, json=_CNBC_JSON)
        async with httpx.AsyncClient() as client:
            bars = await fetch_vkospi_daily(client, "5Y")
    assert len(bars) == 2  # 깨진 봉 제외
    assert bars[0].price_date == date(2026, 1, 1)
    assert bars[1].price_date == date(2026, 6, 23)  # 오래된 순 정렬
    assert bars[1].close_price == 89.41
    assert bars[0].open_price == 24.42


@pytest.mark.asyncio
async def test_fetch_vkospi_daily_empty_on_error():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_CNBC_URL_RE).respond(500)
        async with httpx.AsyncClient() as client:
            bars = await fetch_vkospi_daily(client, "1M")
    assert bars == []
