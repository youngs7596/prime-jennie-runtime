"""MacroStateStore 테스트 — set/get 왕복 + MG08 24h stale detection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from prime_jennie_runtime.slow_loop.macro.state_store import (
    MacroCurrentState,
    MacroStateStore,
)


def _state(age: timedelta = timedelta(seconds=0)) -> MacroCurrentState:
    return MacroCurrentState(
        macro_run_id="macro_20260416_0800",
        gate="open",
        size_multiplier=0.75,
        generated_at=datetime.now(UTC) - age,
    )


@pytest.mark.asyncio
async def test_set_and_get_roundtrip(fake_redis):
    store = MacroStateStore(fake_redis)
    original = _state()
    await store.set(original)

    got = await store.get()
    assert got is not None
    assert got.macro_run_id == original.macro_run_id
    assert got.gate == original.gate
    assert got.size_multiplier == original.size_multiplier


@pytest.mark.asyncio
async def test_get_none_when_key_missing(fake_redis):
    store = MacroStateStore(fake_redis)
    assert await store.get() is None


@pytest.mark.asyncio
async def test_mg08_fresh_not_stale(fake_redis):
    """갓 저장된 상태는 stale 아님."""
    store = MacroStateStore(fake_redis)
    await store.set(_state())
    assert await store.is_stale() is False


@pytest.mark.asyncio
async def test_mg08_over_24h_stale(fake_redis):
    """25시간 전 상태는 stale."""
    store = MacroStateStore(fake_redis)
    await store.set(_state(age=timedelta(hours=25)))
    assert await store.is_stale() is True


@pytest.mark.asyncio
async def test_mg08_exactly_24h_is_stale(fake_redis):
    """정확히 24시간 경과: threshold >= 이므로 stale."""
    store = MacroStateStore(fake_redis)
    await store.set(_state(age=timedelta(hours=24)))
    assert await store.is_stale() is True


@pytest.mark.asyncio
async def test_mg08_missing_state_is_stale(fake_redis):
    """상태 자체 없으면 stale (Strategy Engine 발행 중단)."""
    store = MacroStateStore(fake_redis)
    assert await store.is_stale() is True


@pytest.mark.asyncio
async def test_is_stale_with_explicit_now(fake_redis):
    """now를 명시해서 판정."""
    store = MacroStateStore(fake_redis)
    ref = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)
    state = MacroCurrentState(
        macro_run_id="m1",
        gate="open",
        size_multiplier=0.5,
        generated_at=datetime(2026, 4, 16, 8, 0, 0, tzinfo=UTC),
    )
    await store.set(state)
    assert await store.is_stale(now=ref) is True

    ref_fresh = datetime(2026, 4, 16, 10, 0, 0, tzinfo=UTC)
    assert await store.is_stale(now=ref_fresh) is False
