"""control.audit 단위 테스트 — record/read decode_responses 양쪽 모드."""

from __future__ import annotations

from datetime import UTC, datetime

import fakeredis.aioredis
import pytest

from prime_jennie_runtime.control.audit import (
    AUDIT_FLAGS,
    read_all,
    read_change,
    record_change,
)


@pytest.mark.asyncio
async def test_record_and_read_change_roundtrip_decode_false():
    r = fakeredis.aioredis.FakeRedis(decode_responses=False)
    ts = datetime(2026, 5, 25, 14, 30, tzinfo=UTC)
    await record_change(r, "stop", "ui:alice", "panic", timestamp=ts)
    rec = await read_change(r, "stop")
    assert rec == {
        "timestamp": "2026-05-25T14:30:00+00:00",
        "triggered_by": "ui:alice",
        "reason": "panic",
    }


@pytest.mark.asyncio
async def test_record_and_read_change_roundtrip_decode_true():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    ts = datetime(2026, 5, 24, 14, 30, tzinfo=UTC)
    await record_change(
        r, "stop", "auto:slow-loop-hb-expired", "slow-loop heartbeat 만료", timestamp=ts
    )
    rec = await read_change(r, "stop")
    assert rec is not None
    assert rec["timestamp"] == "2026-05-24T14:30:00+00:00"
    assert rec["triggered_by"] == "auto:slow-loop-hb-expired"
    assert rec["reason"] == "slow-loop heartbeat 만료"


@pytest.mark.asyncio
async def test_read_change_missing_returns_none():
    r = fakeredis.aioredis.FakeRedis(decode_responses=False)
    assert await read_change(r, "stop") is None


@pytest.mark.asyncio
async def test_read_change_empty_reason_normalized_to_none():
    r = fakeredis.aioredis.FakeRedis(decode_responses=False)
    await record_change(r, "pause", "ui:anon", reason=None)
    rec = await read_change(r, "pause")
    assert rec is not None and rec["reason"] is None


@pytest.mark.asyncio
async def test_read_all_returns_entry_for_every_flag():
    r = fakeredis.aioredis.FakeRedis(decode_responses=False)
    await record_change(r, "stop", "ui:alice", "panic")
    audit = await read_all(r)
    # 모든 canonical flag 가 키로 존재 — 미기록 자리는 None
    assert set(audit.keys()) == set(AUDIT_FLAGS)
    assert audit["stop"] is not None
    assert audit["pause"] is None
    assert audit["dryrun"] is None
    assert audit["liquidate_armed"] is None
