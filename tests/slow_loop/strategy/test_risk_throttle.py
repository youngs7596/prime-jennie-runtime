"""RedisRiskThrottleSnapshot — fast_loop 가 Redis 에 적재한 level 을 읽는 단위 테스트.

fast_loop 의 IntradayRiskThrottle 와 동일한 5단계 multiplier 매핑을 검증.
"""

from __future__ import annotations

import pytest

from prime_jennie_runtime.fast_loop.risk_throttle import (
    LEVEL_MULTIPLIER,
    REDIS_LEVEL_KEY,
)
from prime_jennie_runtime.slow_loop.strategy.risk_throttle import (
    RedisRiskThrottleSnapshot,
)


@pytest.mark.asyncio
async def test_default_when_redis_empty(fake_redis):
    """Redis 키가 비어있으면 default (1.0) 반환."""
    snap = RedisRiskThrottleSnapshot(fake_redis)
    await snap.refresh()
    assert snap.current_multiplier() == 1.0
    assert snap.current_level == "NORMAL"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "level,expected_mult",
    [
        ("NORMAL", 1.0),
        ("CAUTION", 0.8),
        ("WARNING", 0.6),
        ("DANGER", 0.3),
        ("CRITICAL", 0.0),
    ],
)
async def test_reflects_level_5_steps(fake_redis, level, expected_mult):
    """fast_loop 5단계 level 별 multiplier 가 그대로 반영됨."""
    await fake_redis.set(REDIS_LEVEL_KEY, level)
    snap = RedisRiskThrottleSnapshot(fake_redis)
    await snap.refresh()
    assert snap.current_level == level
    assert snap.current_multiplier() == pytest.approx(expected_mult, abs=1e-9)
    # fast_loop 매핑과 일치 — 두 곳의 정책이 어긋날 수 없음
    assert snap.current_multiplier() == LEVEL_MULTIPLIER[level]


@pytest.mark.asyncio
async def test_unknown_level_falls_back_to_default(fake_redis):
    """알 수 없는 level 값 → default 로 fallback (fail-open)."""
    await fake_redis.set(REDIS_LEVEL_KEY, "BOGUS")
    snap = RedisRiskThrottleSnapshot(fake_redis)
    await snap.refresh()
    assert snap.current_level == "NORMAL"
    assert snap.current_multiplier() == 1.0


@pytest.mark.asyncio
async def test_redis_get_error_falls_back(fake_redis):
    """Redis get 실패 시 default 로 fallback. raise 안 함."""

    class _BrokenRedis:
        async def get(self, *_a, **_kw):
            raise RuntimeError("conn fail")

    snap = RedisRiskThrottleSnapshot(_BrokenRedis(), default_multiplier=1.0)
    await snap.refresh()  # 예외 없이 통과
    assert snap.current_multiplier() == 1.0


@pytest.mark.asyncio
async def test_refresh_picks_up_updates(fake_redis):
    """fast_loop 가 도중에 level 을 갱신하면 다음 refresh 에서 반영됨."""
    snap = RedisRiskThrottleSnapshot(fake_redis)

    await fake_redis.set(REDIS_LEVEL_KEY, "CAUTION")
    await snap.refresh()
    assert snap.current_multiplier() == 0.8

    await fake_redis.set(REDIS_LEVEL_KEY, "DANGER")
    await snap.refresh()
    assert snap.current_multiplier() == 0.3


@pytest.mark.asyncio
async def test_current_multiplier_is_sync(fake_redis):
    """current_multiplier() 는 sync 인터페이스 — StrategyEngine 호환."""
    await fake_redis.set(REDIS_LEVEL_KEY, "WARNING")
    snap = RedisRiskThrottleSnapshot(fake_redis)
    await snap.refresh()
    # 동기 호출이 가능해야 함 (await 없이)
    result = snap.current_multiplier()
    assert result == pytest.approx(0.6, abs=1e-9)
