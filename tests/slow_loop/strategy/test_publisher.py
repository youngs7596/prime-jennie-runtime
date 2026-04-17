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
