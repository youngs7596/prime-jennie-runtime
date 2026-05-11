"""Intraday Risk Throttle 스냅샷 프로토콜.

Track C(fast loop)가 실제 RiskThrottle을 구현한다. Track B는 **시트 발행 시점**에
스냅샷 값만 읽어 `size.risk_multiplier`에 고정한다 (POSITION_SHEET_SPEC §3.1).

slow_loop 는 fast_loop 의 `IntradayRiskThrottle` 가 Redis 키
`intraday:risk:level` 에 적재한 최신 level 을 읽어 동일한 5단계 multiplier 를
시트 발행 시점에 반영한다. process 가 분리되어 인스턴스 공유는 불가하지만 Redis
를 통한 decoupled share 로 동일 정책을 보장한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import redis.asyncio as aioredis

from prime_jennie_runtime.fast_loop.risk_throttle import (
    LEVEL_MULTIPLIER,
    REDIS_LEVEL_KEY,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class RiskThrottleSnapshot(Protocol):
    """현재 risk_multiplier (0.0 ~ 1.0)을 돌려주는 프로토콜."""

    def current_multiplier(self) -> float: ...


@dataclass(frozen=True)
class NoOpRiskThrottle:
    """항상 1.0을 반환하는 stub. Phase 1 기본값 / Redis 미구성 환경."""

    def current_multiplier(self) -> float:
        return 1.0


@dataclass(frozen=True)
class FixedRiskThrottle:
    """테스트용 고정값 throttle."""

    value: float

    def current_multiplier(self) -> float:
        return self.value


class RedisRiskThrottleSnapshot:
    """fast_loop 가 Redis 에 적재한 `intraday:risk:level` 을 polling 으로 읽어
    multiplier 로 변환하는 스냅샷.

    `current_multiplier()` 는 sync 인터페이스 (StrategyEngine 호환). 매 slow_loop
    실행 직전에 `await refresh()` 를 한 번 호출해 캐시를 갱신한다. Redis 미가용 /
    키 부재 시 fail-open (multiplier=1.0).

    fast_loop 의 IntradayRiskThrottle 와 동일 `LEVEL_MULTIPLIER` 매핑을 그대로
    재사용 — 두 곳의 정책이 어긋날 위험을 원천 차단.
    """

    def __init__(
        self,
        redis_client: aioredis.Redis,
        default_multiplier: float = 1.0,
    ) -> None:
        self._client = redis_client
        self._default = default_multiplier
        self._current_level: str = "NORMAL"
        self._current_mult: float = default_multiplier

    def current_multiplier(self) -> float:
        return self._current_mult

    @property
    def current_level(self) -> str:
        return self._current_level

    async def refresh(self) -> None:
        """Redis 에서 최신 level 을 읽어 캐시 갱신. 실패/부재 시 default 로 fallback."""
        try:
            raw = await self._client.get(REDIS_LEVEL_KEY)
        except Exception:
            logger.exception("RedisRiskThrottleSnapshot: get failed — fallback to default")
            self._current_level = "NORMAL"
            self._current_mult = self._default
            return

        if raw is None:
            self._current_level = "NORMAL"
            self._current_mult = self._default
            return

        level = raw.decode() if isinstance(raw, bytes) else str(raw)
        if level not in LEVEL_MULTIPLIER:
            logger.warning(
                "RedisRiskThrottleSnapshot: unknown level=%r — fallback to default", level
            )
            self._current_level = "NORMAL"
            self._current_mult = self._default
            return

        self._current_level = level
        self._current_mult = LEVEL_MULTIPLIER[level]


__all__ = [
    "FixedRiskThrottle",
    "NoOpRiskThrottle",
    "RedisRiskThrottleSnapshot",
    "RiskThrottleSnapshot",
]
