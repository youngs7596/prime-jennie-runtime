"""PositionSheetPublisher 테스트."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from prime_jennie_runtime.infra.redis_streams import (
    STREAM_POSITION_SHEETS,
    STREAM_POSITION_SHEETS_DLQ,
)
from prime_jennie_runtime.position_sheet.schema import KST, PositionSheet
from prime_jennie_runtime.slow_loop.strategy.publisher import PositionSheetPublisher


def _valid_sheet() -> PositionSheet:
    now = datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST)
    return PositionSheet(
        sheet_id="ps_20260416_005930_abcd",
        generated_at=now,
        valid_until=now + timedelta(hours=6),
        ticker="005930",
        strategy_tag="SECTOR_MOMENTUM",
        size={
            "base_pct": 0.05,
            "macro_multiplier": 1.0,
            "risk_multiplier": 1.0,
            "final_pct": 0.05,
            "max_notional_krw": 5_000_000,
        },
        entry={
            "trigger": "limit",
            "price": 71200,
            "valid_until": now + timedelta(hours=1),
        },
        exit={
            "rules": [
                {"type": "fixed_sl", "pct": 0.04},
                {"type": "time_stop", "mode": "eod"},
            ],
        },
        provenance={
            "scout_run_id": "scout_20260416_0900",
            "scout_code_hash": "sha256:test",
            "scout_hypothesis": "테스트",
            "macro_state_snapshot": {
                "gate": "open",
                "size_multiplier": 1.0,
                "gate_run_id": "macro_20260416_0800",
            },
            "strategy_policy_version": "v3.0.1",
            "generated_by": "test",
        },
    )


@pytest.mark.asyncio
async def test_publish_valid_sheet(fake_redis):
    publisher = PositionSheetPublisher(fake_redis)
    msg_id = await publisher.publish(_valid_sheet())
    assert msg_id

    length = await fake_redis.xlen(STREAM_POSITION_SHEETS)
    assert length == 1


@pytest.mark.asyncio
async def test_dlq_for_raw_payload(fake_redis):
    publisher = PositionSheetPublisher(fake_redis)
    await publisher.send_raw_to_dlq(
        raw_payload='{"broken": "schema"}',
        error="missing required field",
    )
    dlq_len = await fake_redis.xlen(STREAM_POSITION_SHEETS_DLQ)
    assert dlq_len == 1


@pytest.mark.asyncio
async def test_publish_persists_to_db_before_stream(fake_redis):
    """db_engine 주입 시 position_sheets upsert 가 stream publish 전에 수행.

    이게 없으면 slow_loop 의 후행 `update_candidate_promotion` 이 FK 위반 (sheet_id
    not in position_sheets) 을 일으킨다 — 2026-04-19 smoke 에서 확인된 regression.
    """
    executed: list[dict] = []

    class _FakeConn:
        async def execute(self, stmt, params):
            executed.append({"stmt": str(stmt), "params": params})

    class _FakeBegin:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, *exc):
            return None

    class _FakeEngine:
        def begin(self):
            return _FakeBegin()

    publisher = PositionSheetPublisher(fake_redis, db_engine=_FakeEngine())
    await publisher.publish(_valid_sheet())

    # DB upsert 1회 호출 + INSERT INTO position_sheets 포함
    assert len(executed) == 1
    assert "INSERT INTO position_sheets" in executed[0]["stmt"]
    assert executed[0]["params"]["sid"] == "ps_20260416_005930_abcd"
    assert executed[0]["params"]["tk"] == "005930"

    # stream 발행도 성공
    length = await fake_redis.xlen(STREAM_POSITION_SHEETS)
    assert length == 1


@pytest.mark.asyncio
async def test_publish_without_db_engine_is_noop(fake_redis):
    """db_engine 미주입 (테스트/dev) 시 DB 경로 skip 하고 stream 발행만 수행."""
    publisher = PositionSheetPublisher(fake_redis, db_engine=None)
    msg_id = await publisher.publish(_valid_sheet())
    assert msg_id
    length = await fake_redis.xlen(STREAM_POSITION_SHEETS)
    assert length == 1


@pytest.mark.asyncio
async def test_publish_emit_stream_false_persists_but_skips_stream(fake_redis):
    """emit_stream=False → position_sheets DB upsert 는 수행, stream emit 만 skip.

    control STOP/PAUSE 중 slow_loop 가 쓰는 경로 — 분석·DB영속은 유지하되
    fast_loop 핸드오프(v3:position_sheets stream)만 차단한다.
    """
    executed: list[dict] = []

    class _FakeConn:
        async def execute(self, stmt, params):
            executed.append({"stmt": str(stmt), "params": params})

    class _FakeBegin:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, *exc):
            return None

    class _FakeEngine:
        def begin(self):
            return _FakeBegin()

    publisher = PositionSheetPublisher(fake_redis, db_engine=_FakeEngine())
    msg_id = await publisher.publish(_valid_sheet(), emit_stream=False)

    # stream 발행 skip — 빈 문자열 반환, stream 길이 0
    assert msg_id == ""
    assert await fake_redis.xlen(STREAM_POSITION_SHEETS) == 0
    # DB 영속은 그대로 수행
    assert len(executed) == 1
    assert "INSERT INTO position_sheets" in executed[0]["stmt"]


@pytest.mark.asyncio
async def test_publish_emit_stream_true_is_default(fake_redis):
    """emit_stream 기본값 True — 명시 안 하면 기존대로 stream 발행."""
    publisher = PositionSheetPublisher(fake_redis, db_engine=None)
    msg_id = await publisher.publish(_valid_sheet())
    assert msg_id
    assert await fake_redis.xlen(STREAM_POSITION_SHEETS) == 1
