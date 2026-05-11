"""ControlCommandConsumer.apply — 각 명령이 올바른 state 전이를 일으키는지."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from minyoung_mah import CollectingObserver

from prime_jennie_runtime.control.consumer import ControlCommandConsumer
from prime_jennie_runtime.control.state import SystemState
from prime_jennie_runtime.telegram_bot.control import (
    STATE_KEY_DRYRUN,
    STATE_KEY_LIQUIDATE_ARMED,
    STATE_KEY_PAUSE,
    STATE_KEY_STOP,
    V2_KEY_PAUSE,
    V2_KEY_STOP,
    ControlCommand,
)


def _cmd(kind: str, **extras) -> ControlCommand:
    return ControlCommand(
        kind=kind,  # type: ignore[arg-type]
        issued_at=datetime(2026, 4, 17, tzinfo=UTC),
        issued_by="telegram:1001",
        **extras,
    )


# ---------- emergency_stop ----------


@pytest.mark.asyncio
async def test_emergency_stop_sets_stop_and_pause(fake_redis):
    c = ControlCommandConsumer(fake_redis, consumer_name="test")
    await c.apply(_cmd("emergency_stop", reason="telegram_emergency"))
    snap = await SystemState(fake_redis).snapshot()
    assert snap.stopped is True
    assert snap.pause_reason == "telegram_emergency"
    assert snap.entry_allowed is False


@pytest.mark.asyncio
async def test_emergency_stop_default_reason(fake_redis):
    c = ControlCommandConsumer(fake_redis, consumer_name="test")
    await c.apply(_cmd("emergency_stop"))
    pause = await fake_redis.get(STATE_KEY_PAUSE)
    assert pause == b"emergency_stop"


# ---------- pause / resume ----------


@pytest.mark.asyncio
async def test_pause_sets_pause_only_not_stop(fake_redis):
    c = ControlCommandConsumer(fake_redis, consumer_name="test")
    await c.apply(_cmd("pause", reason="technical_risk"))
    snap = await SystemState(fake_redis).snapshot()
    assert snap.paused
    assert snap.pause_reason == "technical_risk"
    assert not snap.stopped


@pytest.mark.asyncio
async def test_resume_clears_both_stop_and_pause(fake_redis):
    await fake_redis.set(STATE_KEY_STOP, "1")
    await fake_redis.set(STATE_KEY_PAUSE, "telegram_emergency")
    c = ControlCommandConsumer(fake_redis, consumer_name="test")
    await c.apply(_cmd("resume"))
    snap = await SystemState(fake_redis).snapshot()
    assert not snap.stopped
    assert not snap.paused
    assert snap.entry_allowed is True


@pytest.mark.asyncio
async def test_resume_on_clean_state_is_idempotent(fake_redis):
    c = ControlCommandConsumer(fake_redis, consumer_name="test")
    await c.apply(_cmd("resume"))  # nothing to clear
    snap = await SystemState(fake_redis).snapshot()
    assert snap.entry_allowed is True


@pytest.mark.asyncio
async def test_resume_also_clears_v2_trading_flags(fake_redis):
    """v2 호환 키 (trading_flags:stop / pause) 도 함께 DEL — Phase 2-6 공존 정리."""
    await fake_redis.set(STATE_KEY_STOP, "1")
    await fake_redis.set(STATE_KEY_PAUSE, "telegram_emergency")
    await fake_redis.set(V2_KEY_STOP, "1")
    await fake_redis.set(V2_KEY_PAUSE, "emergency_stop")

    c = ControlCommandConsumer(fake_redis, consumer_name="test")
    await c.apply(_cmd("resume"))

    # v3 키
    assert await fake_redis.get(STATE_KEY_STOP) is None
    assert await fake_redis.get(STATE_KEY_PAUSE) is None
    # v2 호환 키 — 함께 정리되어야 함
    assert await fake_redis.get(V2_KEY_STOP) is None
    assert await fake_redis.get(V2_KEY_PAUSE) is None


# ---------- set_dryrun ----------


@pytest.mark.asyncio
async def test_set_dryrun_on(fake_redis):
    c = ControlCommandConsumer(fake_redis, consumer_name="test")
    await c.apply(_cmd("set_dryrun", payload={"enabled": True}))
    snap = await SystemState(fake_redis).snapshot()
    assert snap.dryrun


@pytest.mark.asyncio
async def test_set_dryrun_off_deletes_key(fake_redis):
    await fake_redis.set(STATE_KEY_DRYRUN, "1")
    c = ControlCommandConsumer(fake_redis, consumer_name="test")
    await c.apply(_cmd("set_dryrun", payload={"enabled": False}))
    snap = await SystemState(fake_redis).snapshot()
    assert not snap.dryrun


@pytest.mark.asyncio
async def test_set_dryrun_default_payload_is_off(fake_redis):
    """payload 에 enabled 가 없으면 False (OFF) 로 해석."""
    await fake_redis.set(STATE_KEY_DRYRUN, "1")
    c = ControlCommandConsumer(fake_redis, consumer_name="test")
    await c.apply(_cmd("set_dryrun"))
    snap = await SystemState(fake_redis).snapshot()
    assert not snap.dryrun


# ---------- liquidate arm / disarm ----------


@pytest.mark.asyncio
async def test_liquidate_arm_sets_flag(fake_redis):
    c = ControlCommandConsumer(fake_redis, consumer_name="test")
    await c.apply(_cmd("liquidate_arm"))
    snap = await SystemState(fake_redis).snapshot()
    assert snap.liquidate_armed


@pytest.mark.asyncio
async def test_liquidate_disarm_clears_flag(fake_redis):
    await fake_redis.set(STATE_KEY_LIQUIDATE_ARMED, "1")
    c = ControlCommandConsumer(fake_redis, consumer_name="test")
    await c.apply(_cmd("liquidate_disarm"))
    snap = await SystemState(fake_redis).snapshot()
    assert not snap.liquidate_armed


# ---------- observer event ----------


@pytest.mark.asyncio
async def test_apply_emits_pj_control_applied(fake_redis):
    observer = CollectingObserver()
    c = ControlCommandConsumer(fake_redis, consumer_name="test", observer=observer)
    await c.apply(_cmd("pause", reason="x"))
    names = [e.name for e in observer.events]
    assert "pj.control.applied" in names


# ---------- emergency_stop → resume round trip ----------


@pytest.mark.asyncio
async def test_emergency_then_resume_restores_entry(fake_redis):
    c = ControlCommandConsumer(fake_redis, consumer_name="test")
    await c.apply(_cmd("emergency_stop", reason="panic"))
    assert not (await SystemState(fake_redis).snapshot()).entry_allowed
    await c.apply(_cmd("resume"))
    assert (await SystemState(fake_redis).snapshot()).entry_allowed
