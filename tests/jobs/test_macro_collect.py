"""`macro_collect_global` 스모크 — 네이버/Yahoo/BOK mock.

핵심 경로:
- KOSPI/KOSDAQ 지수 정상 수집 → Redis 저장
- 지수 둘 다 실패 시 `MacroCollectError`
- 기존 snapshot 있을 때 global 필드 보존 + 한국 필드만 덮어씀
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.council_macro import (
    MACRO_SNAPSHOT_KEY_PREFIX,
    MacroCollectError,
    macro_collect_global,
)

_INDEX_URL_RE = r"https://m\.stock\.naver\.com/api/index/.*"
_INVESTOR_URL_RE = r"https://finance\.naver\.com/sise/investorDealTrendDay\.naver.*"
_YAHOO_URL_RE = r"https://query1\.finance\.yahoo\.com/.*"


def _index_payload(close: str, change: str, traded_at: str) -> dict:
    return {
        "closePrice": close,
        "fluctuationsRatio": change,
        "localTradedAt": traded_at,
    }


def _yahoo_payload(closes: list[float | None]) -> dict:
    return {
        "chart": {
            "result": [
                {"indicators": {"quote": [{"close": closes}]}},
            ]
        }
    }


@pytest.mark.asyncio
async def test_macro_collect_global_stores_snapshot(fake_redis):
    today = date.today()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_INDEX_URL_RE).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=_index_payload(
                        "3,100.50", "0.8", f"{today.isoformat()}T15:30:00+09:00"
                    ),
                ),
                httpx.Response(
                    200,
                    json=_index_payload(
                        "950.25", "-0.3", f"{today.isoformat()}T15:30:00+09:00"
                    ),
                ),
            ]
        )
        mock.get(url__regex=_INVESTOR_URL_RE).respond(200, text="<html/>")
        mock.get(url__regex=_YAHOO_URL_RE).respond(
            200, json=_yahoo_payload([18.0, 18.5, 19.2])
        )
        async with httpx.AsyncClient() as client:
            snapshot = await macro_collect_global(fake_redis, client)

    assert snapshot["kospi_index"] == 3100.5
    assert snapshot["kosdaq_index"] == 950.25
    assert snapshot["vix"] == 19.2
    assert snapshot["vix_regime"] == "normal"
    assert snapshot["data_sources"] == ["naver"]

    stored = await fake_redis.get(f"{MACRO_SNAPSHOT_KEY_PREFIX}{today.isoformat()}")
    assert stored is not None
    parsed = json.loads(stored)
    assert parsed["kospi_index"] == 3100.5


@pytest.mark.asyncio
async def test_macro_collect_global_raises_when_indexes_fail(fake_redis):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_INDEX_URL_RE).respond(500, json={})
        mock.get(url__regex=_YAHOO_URL_RE).respond(200, json=_yahoo_payload([]))
        async with httpx.AsyncClient() as client:
            with pytest.raises(MacroCollectError, match="No trading data"):
                await macro_collect_global(fake_redis, client)


@pytest.mark.asyncio
async def test_macro_collect_global_preserves_existing_fields(fake_redis):
    today = date.today()
    existing = {
        "snapshot_date": today.isoformat(),
        "us_gdp": 2.5,  # 기존 글로벌 필드 (한국 수집이 건들지 말아야 함)
        "kospi_index": 0.0,  # 구값 — 덮어써야 함
    }
    await fake_redis.set(
        f"{MACRO_SNAPSHOT_KEY_PREFIX}{today.isoformat()}", json.dumps(existing)
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_INDEX_URL_RE).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=_index_payload(
                        "3,200.00", "1.5", f"{today.isoformat()}T15:30:00+09:00"
                    ),
                ),
                httpx.Response(
                    200,
                    json=_index_payload(
                        "900.00", "0.2", f"{today.isoformat()}T15:30:00+09:00"
                    ),
                ),
            ]
        )
        mock.get(url__regex=_INVESTOR_URL_RE).respond(200, text="<html/>")
        mock.get(url__regex=_YAHOO_URL_RE).respond(200, json=_yahoo_payload([]))
        async with httpx.AsyncClient() as client:
            snapshot = await macro_collect_global(fake_redis, client)

    assert snapshot["kospi_index"] == 3200.0  # 갱신
    assert snapshot["us_gdp"] == 2.5  # 보존
