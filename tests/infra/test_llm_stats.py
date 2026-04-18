"""record_llm_call 검증 — fakeredis 기반 키 구조 + 누적."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import fakeredis.aioredis
import pytest

from prime_jennie_runtime.infra.llm_stats import record_llm_call

_KST = timezone(timedelta(hours=9))


@pytest.mark.asyncio
async def test_record_llm_call_writes_expected_key_and_fields():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    ts = datetime(2026, 4, 18, 10, 30, tzinfo=_KST)

    await record_llm_call(
        r,
        service="scout",
        input_tokens=1234,
        output_tokens=567,
        cost_usd=0.0321,
        timestamp=ts,
    )

    data = await r.hgetall("llm:stats:2026-04-18:scout")
    assert data["calls"] == "1"
    assert data["tokens_in"] == "1234"
    assert data["tokens_out"] == "567"
    assert abs(float(data["cost_usd"]) - 0.0321) < 1e-9


@pytest.mark.asyncio
async def test_record_llm_call_accumulates_across_calls():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    ts = datetime(2026, 4, 18, 10, 30, tzinfo=_KST)

    for _ in range(3):
        await record_llm_call(
            r, service="macro", input_tokens=100, output_tokens=50, cost_usd=0.01, timestamp=ts
        )

    data = await r.hgetall("llm:stats:2026-04-18:macro")
    assert data["calls"] == "3"
    assert data["tokens_in"] == "300"
    assert data["tokens_out"] == "150"
    assert abs(float(data["cost_usd"]) - 0.03) < 1e-9


@pytest.mark.asyncio
async def test_record_llm_call_uses_kst_date_boundary():
    """UTC 2026-04-17 23:30 은 KST 2026-04-18 08:30 — 키는 KST 기준 2026-04-18."""
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    ts_utc = datetime(2026, 4, 17, 23, 30, tzinfo=UTC)

    await record_llm_call(r, service="macro", input_tokens=10, output_tokens=5, timestamp=ts_utc)

    # KST 기준 키 존재
    assert await r.hgetall("llm:stats:2026-04-18:macro")
    # UTC 기준 키는 없음
    assert not await r.hgetall("llm:stats:2026-04-17:macro")


@pytest.mark.asyncio
async def test_record_llm_call_skips_cost_when_none():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await record_llm_call(
        r,
        service="scout",
        input_tokens=10,
        output_tokens=5,
        cost_usd=None,
        timestamp=datetime(2026, 4, 18, 10, 0, tzinfo=_KST),
    )
    data = await r.hgetall("llm:stats:2026-04-18:scout")
    assert data["calls"] == "1"
    assert "cost_usd" not in data


@pytest.mark.asyncio
async def test_record_llm_call_noop_when_client_none():
    # redis_client=None 에서도 예외 없이 통과해야 함 — dead path 테스트
    await record_llm_call(None, service="scout", input_tokens=1, output_tokens=1)


@pytest.mark.asyncio
async def test_record_llm_call_naive_timestamp_treated_as_kst():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    # tz 정보 없는 datetime — KST 로 해석
    naive = datetime(2026, 4, 18, 10, 0)
    await record_llm_call(r, service="macro", input_tokens=1, output_tokens=1, timestamp=naive)
    assert await r.hgetall("llm:stats:2026-04-18:macro")
