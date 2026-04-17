"""SystemState reader — snapshot 정확성."""

from __future__ import annotations

import pytest

from prime_jennie_runtime.control.state import SystemState
from prime_jennie_runtime.telegram_bot.control import (
    STATE_KEY_DRYRUN,
    STATE_KEY_LIQUIDATE_ARMED,
    STATE_KEY_PAUSE,
    STATE_KEY_STOP,
)


@pytest.mark.asyncio
async def test_snapshot_empty_redis_defaults(fake_redis):
    state = SystemState(fake_redis)
    snap = await state.snapshot()
    assert not snap.stopped
    assert not snap.paused
    assert snap.pause_reason is None
    assert not snap.dryrun
    assert not snap.liquidate_armed
    assert snap.entry_allowed is True


@pytest.mark.asyncio
async def test_snapshot_with_pause_reason(fake_redis):
    await fake_redis.set(STATE_KEY_PAUSE, "risk_throttle")
    snap = await SystemState(fake_redis).snapshot()
    assert snap.paused
    assert snap.pause_reason == "risk_throttle"
    assert not snap.stopped
    assert snap.entry_allowed is False


@pytest.mark.asyncio
async def test_snapshot_stopped_blocks_entry(fake_redis):
    await fake_redis.set(STATE_KEY_STOP, "1")
    snap = await SystemState(fake_redis).snapshot()
    assert snap.stopped
    assert snap.entry_allowed is False


@pytest.mark.asyncio
async def test_snapshot_dryrun_flag(fake_redis):
    await fake_redis.set(STATE_KEY_DRYRUN, "1")
    snap = await SystemState(fake_redis).snapshot()
    assert snap.dryrun


@pytest.mark.asyncio
async def test_snapshot_liquidate_armed(fake_redis):
    await fake_redis.set(STATE_KEY_LIQUIDATE_ARMED, "1")
    snap = await SystemState(fake_redis).snapshot()
    assert snap.liquidate_armed


@pytest.mark.asyncio
async def test_snapshot_combined_flags_independent(fake_redis):
    await fake_redis.set(STATE_KEY_STOP, "1")
    await fake_redis.set(STATE_KEY_DRYRUN, "1")
    snap = await SystemState(fake_redis).snapshot()
    assert snap.stopped and snap.dryrun
    assert not snap.liquidate_armed
