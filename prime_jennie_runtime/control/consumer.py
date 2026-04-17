"""Control command consumer — `v3:control.commands` → `control.state:*` 반영.

`TypedStreamConsumer` 위에 `ControlCommand` 핸들러를 얹는 얇은 래퍼.
Consumer group 는 `control.runtime` 하나만 둔다 (state 는 멱등이므로 단일 consumer
로 충분). 향후 fast/slow loop 가 별도 상태 복제를 원하면 group 추가.

설계:
- Redis SET/DEL 로 state 키를 전환. TTL 없음 (명시적 resume 전까지 유지).
- emergency_stop → STOP + PAUSE 동시 설정 (v2 policy: stop 이 pause 를 함의).
- resume → STOP + PAUSE 동시 해제 (긴급 복귀).
- set_dryrun → payload.enabled True/False 기반 SET/DEL.
- observer 주입 시 `pj.control.applied` 이벤트 발생 (telemetry).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import redis.asyncio as aioredis
from minyoung_mah import Observer

from prime_jennie_runtime.infra.observer_impl import pj_event
from prime_jennie_runtime.infra.redis_streams import (
    STREAM_CONTROL_COMMANDS,
    TypedStreamConsumer,
)
from prime_jennie_runtime.telegram_bot.control import (
    STATE_KEY_DRYRUN,
    STATE_KEY_LIQUIDATE_ARMED,
    STATE_KEY_PAUSE,
    STATE_KEY_STOP,
    ControlCommand,
)

logger = logging.getLogger(__name__)

CONTROL_GROUP = "control.runtime"


class ControlCommandConsumer:
    """Redis stream `v3:control.commands` → state 반영 + 이벤트."""

    def __init__(
        self,
        redis_client: aioredis.Redis,
        *,
        consumer_name: str,
        observer: Observer | None = None,
        group: str = CONTROL_GROUP,
    ) -> None:
        self._redis = redis_client
        self._observer = observer
        self._consumer = TypedStreamConsumer(
            client=redis_client,
            stream=STREAM_CONTROL_COMMANDS,
            group=group,
            consumer=consumer_name,
            model_class=ControlCommand,
            handler=self.apply,
        )

    async def run(self) -> None:
        await self._consumer.run()

    def stop(self) -> None:
        self._consumer.stop()

    async def apply(self, command: ControlCommand) -> None:
        """단일 명령 적용 — 테스트 / consumer 양쪽에서 직접 호출 가능."""
        handler = _HANDLERS.get(command.kind)
        if handler is None:
            logger.error("unknown control command kind: %s", command.kind)
            return
        await handler(self._redis, command)
        if self._observer is not None:
            await self._observer.emit(
                pj_event(
                    "pj.control.applied",
                    role="control",
                    ok=True,
                    kind=command.kind,
                    issued_by=command.issued_by,
                    reason=command.reason or "",
                )
            )


# ---------------------------------------------------------------------
# 명령별 apply 함수 — async(redis, ControlCommand) → None
# ---------------------------------------------------------------------


async def _emergency_stop(redis: aioredis.Redis, cmd: ControlCommand) -> None:
    await redis.set(STATE_KEY_STOP, b"1")
    await redis.set(STATE_KEY_PAUSE, (cmd.reason or "emergency_stop").encode())


async def _pause(redis: aioredis.Redis, cmd: ControlCommand) -> None:
    await redis.set(STATE_KEY_PAUSE, (cmd.reason or "manual_pause").encode())


async def _resume(redis: aioredis.Redis, cmd: ControlCommand) -> None:
    await redis.delete(STATE_KEY_STOP, STATE_KEY_PAUSE)


async def _set_dryrun(redis: aioredis.Redis, cmd: ControlCommand) -> None:
    enabled = bool(cmd.payload.get("enabled", False))
    if enabled:
        await redis.set(STATE_KEY_DRYRUN, b"1")
    else:
        await redis.delete(STATE_KEY_DRYRUN)


async def _liquidate_arm(redis: aioredis.Redis, cmd: ControlCommand) -> None:
    await redis.set(STATE_KEY_LIQUIDATE_ARMED, b"1")


async def _liquidate_disarm(redis: aioredis.Redis, cmd: ControlCommand) -> None:
    await redis.delete(STATE_KEY_LIQUIDATE_ARMED)


_HANDLERS: dict[str, Callable[[aioredis.Redis, ControlCommand], Awaitable[None]]] = {
    "emergency_stop": _emergency_stop,
    "pause": _pause,
    "resume": _resume,
    "set_dryrun": _set_dryrun,
    "liquidate_arm": _liquidate_arm,
    "liquidate_disarm": _liquidate_disarm,
}


__all__ = ["CONTROL_GROUP", "ControlCommandConsumer"]
