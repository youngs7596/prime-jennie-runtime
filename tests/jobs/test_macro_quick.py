"""`macro_quick` 스모크 — wrapper 가 `macro_collect_global` 경유로 snapshot 을
저장하는지만 확인한다. v2 의 throttle 레이어는 Context 모델이 v3 에 포팅된 뒤
다음 슬라이스에서 검증한다.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.council_macro import (
    MACRO_SNAPSHOT_KEY_PREFIX,
    macro_quick,
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
async def test_macro_quick_delegates_to_collect_global(fake_redis):
    today = date.today()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_INDEX_URL_RE).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=_index_payload(
                        "3,050.00", "0.5", f"{today.isoformat()}T15:30:00+09:00"
                    ),
                ),
                httpx.Response(
                    200,
                    json=_index_payload(
                        "920.00", "-0.2", f"{today.isoformat()}T15:30:00+09:00"
                    ),
                ),
            ]
        )
        mock.get(url__regex=_INVESTOR_URL_RE).respond(200, text="<html/>")
        mock.get(url__regex=_YAHOO_URL_RE).respond(
            200, json=_yahoo_payload([17.0, 17.5, 18.0])
        )
        async with httpx.AsyncClient() as client:
            snapshot = await macro_quick(fake_redis, client)

    assert snapshot["kospi_index"] == 3050.0
    assert snapshot["kosdaq_index"] == 920.0

    stored = await fake_redis.get(f"{MACRO_SNAPSHOT_KEY_PREFIX}{today.isoformat()}")
    assert stored is not None
    parsed = json.loads(stored)
    assert parsed["kospi_index"] == 3050.0
