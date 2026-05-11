"""Control command consumer — `v3:control.commands` → `control.state:*` 반영.

`TypedStreamConsumer` 위에 `ControlCommand` 핸들러를 얹는 얇은 래퍼.
Consumer group 는 `control.runtime` 하나만 둔다 (state 는 멱등이므로 단일 consumer
로 충분). 향후 fast/slow loop 가 별도 상태 복제를 원하면 group 추가.

설계:
- Redis SET/DEL 로 state 키를 전환. TTL 없음 (명시적 resume 전까지 유지).
- emergency_stop → STOP + PAUSE 동시 설정 (v2 policy: stop 이 pause 를 함의).
- resume → STOP + PAUSE 동시 해제 (긴급 복귀).
- set_dryrun → payload.enabled True/False 기반 SET/DEL.
- manual_buy / manual_sell / manual_sellall — kis_client 주입 시 KIS gateway
  로 주문. DRY_RUN 키 ON 이면 실 주문 없이 로그만.
- observer 주입 시 `pj.control.applied` 이벤트 발생 (telemetry).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import redis.asyncio as aioredis
from minyoung_mah import Observer

from prime_jennie_runtime.infra.observer_impl import pj_event
from prime_jennie_runtime.infra.redis_streams import (
    STREAM_CONTROL_COMMANDS,
    TypedStreamConsumer,
)
from prime_jennie_runtime.telegram_bot.control import (
    KEY_FORCED_LIQUIDATION,
    STATE_KEY_DRYRUN,
    STATE_KEY_LIQUIDATE_ARMED,
    STATE_KEY_PAUSE,
    STATE_KEY_STOP,
    V2_KEY_PAUSE,
    V2_KEY_STOP,
    ControlCommand,
)

if TYPE_CHECKING:
    from prime_jennie_runtime.fast_loop.kis_client import KisClient

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
        kis_client: KisClient | None = None,
    ) -> None:
        self._redis = redis_client
        self._observer = observer
        self._kis = kis_client
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
        if command.kind in ("manual_buy", "manual_sell", "manual_sellall"):
            await self._apply_manual_trade(command)
        else:
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

    async def _apply_manual_trade(self, command: ControlCommand) -> None:
        if self._kis is None:
            logger.error(
                "manual trade %s received but ControlConsumer has no kis_client", command.kind
            )
            return
        # 방어적 STOP 체크 — handler 가 publish 직전에 차단하지만 publish 직후
        # STOP 이 들어오는 race window 가 있어 consumer 측에서도 한 번 더 검사.
        stopped = bool(await self._redis.get(STATE_KEY_STOP))
        if stopped:
            logger.warning(
                "manual trade blocked by STOP: kind=%s payload=%s",
                command.kind,
                command.payload,
            )
            return
        # PAUSE 는 manual_buy 만 차단 (manual_sell/sellall 은 청산이라 허용).
        if command.kind == "manual_buy":
            paused = bool(await self._redis.get(STATE_KEY_PAUSE))
            if paused:
                logger.warning("manual_buy blocked by PAUSE: payload=%s", command.payload)
                return
        dryrun = bool(await self._redis.get(STATE_KEY_DRYRUN))
        if dryrun:
            logger.info(
                "DRY_RUN: skipping manual trade kind=%s payload=%s", command.kind, command.payload
            )
            return
        from prime_jennie_runtime.kis_gateway.schemas import OrderRequest

        if command.kind == "manual_buy":
            ticker = str(command.payload.get("ticker", ""))
            qty = int(command.payload.get("quantity", 0))
            if not ticker or qty <= 0:
                logger.warning("manual_buy invalid payload: %s", command.payload)
                return
            try:
                result = await self._kis.buy(
                    OrderRequest(stock_code=ticker, quantity=qty, order_type="market", price=0)
                )
                logger.info(
                    "manual_buy ticker=%s qty=%d success=%s order_no=%s",
                    ticker,
                    qty,
                    result.success,
                    result.order_no,
                )
            except Exception:
                logger.exception("manual_buy failed ticker=%s qty=%d", ticker, qty)

        elif command.kind == "manual_sell":
            ticker = str(command.payload.get("ticker", ""))
            qty = int(command.payload.get("quantity", 0))
            if not ticker or qty <= 0:
                logger.warning("manual_sell invalid payload: %s", command.payload)
                return
            try:
                result = await self._kis.sell(
                    OrderRequest(stock_code=ticker, quantity=qty, order_type="market", price=0)
                )
                logger.info(
                    "manual_sell ticker=%s qty=%d success=%s order_no=%s",
                    ticker,
                    qty,
                    result.success,
                    result.order_no,
                )
            except Exception:
                logger.exception("manual_sell failed ticker=%s qty=%d", ticker, qty)

        elif command.kind == "manual_sellall":
            try:
                state = await self._kis.get_balance()
            except Exception:
                logger.exception("manual_sellall: get_balance failed")
                return
            if not state.positions:
                logger.info("manual_sellall: no positions")
                return
            count = 0
            for p in state.positions:
                try:
                    await self._kis.sell(
                        OrderRequest(
                            stock_code=p.stock_code,
                            quantity=p.quantity,
                            order_type="market",
                            price=0,
                        )
                    )
                    count += 1
                except Exception:
                    logger.exception(
                        "manual_sellall sell failed ticker=%s qty=%d",
                        p.stock_code,
                        p.quantity,
                    )
            logger.info("manual_sellall: sold %d/%d positions", count, len(state.positions))


# ---------------------------------------------------------------------
# 명령별 apply 함수 — async(redis, ControlCommand) → None
# ---------------------------------------------------------------------


async def _emergency_stop(redis: aioredis.Redis, cmd: ControlCommand) -> None:
    await redis.set(STATE_KEY_STOP, b"1")
    await redis.set(STATE_KEY_PAUSE, (cmd.reason or "emergency_stop").encode())


async def _pause(redis: aioredis.Redis, cmd: ControlCommand) -> None:
    await redis.set(STATE_KEY_PAUSE, (cmd.reason or "manual_pause").encode())


async def _resume(redis: aioredis.Redis, cmd: ControlCommand) -> None:
    # v3 키 + v2 호환 키 모두 정리. v2 키는 운영자가 REAL_MODE_MIGRATION_CHECKLIST
    # 따라 수동 SET 하는 관행 — `_resume` 가 v3 키만 지우면 v2 호환 키가 남아
    # 그쪽 환경에 영향 (`SystemState.snapshot` 은 v3 키만 보지만, v2 운영 코드와
    # 공존 기간에 `trading_flags:stop=1` 잔존이 confusing). docs/CONTROL_STATE_KEYS.md
    # 참조. v2 deprecate 시 두 키는 import + 본 delete 호출 모두 제거.
    await redis.delete(STATE_KEY_STOP, STATE_KEY_PAUSE, V2_KEY_STOP, V2_KEY_PAUSE)


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


async def _liquidate_add(redis: aioredis.Redis, cmd: ControlCommand) -> None:
    ticker = str(cmd.payload.get("ticker", "")).strip()
    if not ticker:
        logger.warning("liquidate_add missing ticker payload")
        return
    await redis.sadd(KEY_FORCED_LIQUIDATION, ticker.encode())


async def _liquidate_remove(redis: aioredis.Redis, cmd: ControlCommand) -> None:
    ticker = str(cmd.payload.get("ticker", "")).strip()
    if not ticker:
        logger.warning("liquidate_remove missing ticker payload")
        return
    await redis.srem(KEY_FORCED_LIQUIDATION, ticker.encode())


async def _liquidate_clear(redis: aioredis.Redis, cmd: ControlCommand) -> None:
    await redis.delete(KEY_FORCED_LIQUIDATION, STATE_KEY_LIQUIDATE_ARMED)


_HANDLERS: dict[str, Callable[[aioredis.Redis, ControlCommand], Awaitable[None]]] = {
    "emergency_stop": _emergency_stop,
    "pause": _pause,
    "resume": _resume,
    "set_dryrun": _set_dryrun,
    "liquidate_arm": _liquidate_arm,
    "liquidate_disarm": _liquidate_disarm,
    "liquidate_add": _liquidate_add,
    "liquidate_remove": _liquidate_remove,
    "liquidate_clear": _liquidate_clear,
}


__all__ = ["CONTROL_GROUP", "ControlCommandConsumer"]
