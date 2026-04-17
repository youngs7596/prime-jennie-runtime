"""System state — ``control.state:*`` Redis 키의 읽기 전용 뷰.

fast/slow loop 가 진입/청산 결정 직전에 ``SystemState.snapshot()`` 으로
현재 제어 상태를 조회. 쓰기는 ``ControlCommandConsumer`` 만 담당.
"""

from __future__ import annotations

from dataclasses import dataclass

import redis.asyncio as aioredis

from prime_jennie_runtime.telegram_bot.control import (
    STATE_KEY_DRYRUN,
    STATE_KEY_LIQUIDATE_ARMED,
    STATE_KEY_PAUSE,
    STATE_KEY_STOP,
)


@dataclass(frozen=True)
class SystemStateSnapshot:
    """한 시점의 제어 상태. 읽은 순간 캡쳐된 값이므로 이후 변경은 재조회 필요."""

    stopped: bool
    pause_reason: str | None  # None 이면 미일시정지
    dryrun: bool
    liquidate_armed: bool

    @property
    def paused(self) -> bool:
        return self.pause_reason is not None

    @property
    def entry_allowed(self) -> bool:
        """신규 진입이 허용되는지 — stop/pause 어느 쪽이든 걸리면 불가."""
        return not (self.stopped or self.paused)


class SystemState:
    """Redis 기반 read-only 상태 뷰."""

    def __init__(self, redis_client: aioredis.Redis) -> None:
        self._redis = redis_client

    async def snapshot(self) -> SystemStateSnapshot:
        """mget 으로 4 키를 한 번에 읽어 스냅샷."""
        raw = await self._redis.mget(
            STATE_KEY_STOP,
            STATE_KEY_PAUSE,
            STATE_KEY_DRYRUN,
            STATE_KEY_LIQUIDATE_ARMED,
        )
        stop, pause, dry, armed = raw
        return SystemStateSnapshot(
            stopped=_truthy(stop),
            pause_reason=_decode(pause),
            dryrun=_truthy(dry),
            liquidate_armed=_truthy(armed),
        )


def _decode(v: bytes | str | None) -> str | None:
    if v is None:
        return None
    if isinstance(v, bytes):
        return v.decode()
    return str(v)


def _truthy(v: bytes | str | None) -> bool:
    if v is None:
        return False
    decoded = _decode(v)
    return bool(decoded) and decoded != "0"


__all__ = ["SystemState", "SystemStateSnapshot"]
