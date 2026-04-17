"""`council_macro.macro_validate_store` 단위 테스트 — fakeredis.

v2 `/jobs/macro-validate-store` 의 분기를 그대로 검증:
- 최근 7일 이내 스냅샷 발견
- 없으면 ERROR
- 필수 필드 (`kospi_index`, `kosdaq_index`) 누락 시 ERROR
- 값 범위 (KOSPI 1000~10000, KOSDAQ 300~3000) 이탈은 warning
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from prime_jennie_runtime.jobs.council_macro import (
    MACRO_SNAPSHOT_KEY_PREFIX,
    MacroValidationError,
    macro_validate_store,
)


def _key(d: date) -> str:
    return f"{MACRO_SNAPSHOT_KEY_PREFIX}{d.isoformat()}"


@pytest.mark.asyncio
async def test_validate_ok_returns_summary(fake_redis):
    today = date.today()
    payload = {
        "kospi_index": 2800.5,
        "kosdaq_index": 900.0,
        "vix": 18.0,
        "snapshot_date": today.isoformat(),
        "data_sources": ["naver"],
    }
    await fake_redis.set(_key(today), json.dumps(payload))
    summary = await macro_validate_store(fake_redis)
    assert summary["key"].endswith(today.isoformat())
    assert summary["warnings"] == []
    assert summary["fields_present"] == 4  # data_sources 제외


@pytest.mark.asyncio
async def test_validate_falls_back_to_earlier_day(fake_redis):
    three_days_ago = date.today() - timedelta(days=3)
    payload = {"kospi_index": 2500.0, "kosdaq_index": 800.0}
    await fake_redis.set(_key(three_days_ago), json.dumps(payload))
    summary = await macro_validate_store(fake_redis)
    assert summary["key"].endswith(three_days_ago.isoformat())


@pytest.mark.asyncio
async def test_validate_no_snapshot_raises(fake_redis):
    with pytest.raises(MacroValidationError, match="No macro snapshot"):
        await macro_validate_store(fake_redis)


@pytest.mark.asyncio
async def test_validate_missing_required_raises(fake_redis):
    today = date.today()
    await fake_redis.set(_key(today), json.dumps({"vix": 20.0}))
    with pytest.raises(MacroValidationError, match="missing fields"):
        await macro_validate_store(fake_redis)


@pytest.mark.asyncio
async def test_validate_out_of_range_adds_warning(fake_redis):
    today = date.today()
    payload = {"kospi_index": 42.0, "kosdaq_index": 999.0}
    await fake_redis.set(_key(today), json.dumps(payload))
    summary = await macro_validate_store(fake_redis)
    assert any("KOSPI" in w for w in summary["warnings"])
    assert not any("KOSDAQ" in w for w in summary["warnings"])
