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
- manual_sell / manual_sellall 의 v3 state 정합: tracker / recorder /
  sheet_fetcher / notifier 가 주입되면 KIS 체결 후 exit_executor 와 동일한
  책임 수행 (PositionTracker close, executions INSERT, positions update,
  TradeNotification, ExitFilledEvent publish). 미주입 시 KIS 호출 + log 만
  (v3 외부 보유 종목 호환). 2026-05-15 audit D1 / manual_sell silent gap
  fix.
- observer 주입 시 `pj.control.applied` 이벤트 발생 (telemetry).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import redis.asyncio as aioredis
from minyoung_mah import Observer

from prime_jennie_runtime.control.audit import record_change
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
    from prime_jennie_runtime.fast_loop.notifier import Notifier
    from prime_jennie_runtime.fast_loop.persistence import PostgresTradeRecorder
    from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker
    from prime_jennie_runtime.position_sheet.schema import PositionSheet

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
        tracker: PositionTracker | None = None,
        recorder: PostgresTradeRecorder | None = None,
        sheet_fetcher: Callable[[str], Awaitable[list[PositionSheet]]] | None = None,
        notifier: Notifier | None = None,
        max_confirm_retries: int = 5,
        confirm_interval: float = 2.0,
    ) -> None:
        self._redis = redis_client
        self._observer = observer
        self._kis = kis_client
        # v3 state 정합용 — 모두 선택 의존성. 미주입 시 KIS 호출만 (legacy 동작).
        self._tracker = tracker
        self._recorder = recorder
        self._sheet_fetcher = sheet_fetcher
        self._notifier = notifier
        self._max_confirm_retries = max_confirm_retries
        self._confirm_interval = confirm_interval
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
        if command.kind == "adopt_position":
            await self._apply_adopt_position(command)
        elif command.kind in ("manual_buy", "manual_sell", "manual_sellall"):
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

    async def _apply_adopt_position(self, command: ControlCommand) -> None:
        """실보유 종목 편입 — in-process PositionTracker 등록 (사람-승인 매매 §3-2).

        시트는 telegram handler 가 이미 position_sheets 에 영속했다. 여기서는
        tracker 등록만 — register 가 Redis 영속까지 같이 하므로 fast-loop 재기동
        후에도 load_from_redis 로 복원된다. 매매가 아니므로 STOP/PAUSE 무관
        (발동 시 주문은 ExitExecutor 의 기존 게이트가 차단).
        """
        from prime_jennie_runtime.fast_loop.domain import PositionState

        if self._tracker is None:
            logger.error("adopt_position received but no tracker (fast-loop 전용 명령)")
            return
        p = command.payload
        sheet_id = str(p.get("sheet_id", ""))
        ticker = str(p.get("ticker", ""))
        try:
            entry_price = float(p.get("entry_price", 0))
            quantity = int(p.get("quantity", 0))
        except (TypeError, ValueError):
            entry_price, quantity = 0.0, 0
        if not sheet_id or not ticker or entry_price <= 0 or quantity <= 0:
            logger.warning("adopt_position invalid payload: %s", command.payload)
            return
        if self._tracker.get(sheet_id) is not None:
            logger.info("adopt_position duplicate sheet_id=%s — skip", sheet_id)
            return
        entered_raw = p.get("entered_at")
        try:
            entered_at = datetime.fromisoformat(str(entered_raw))
        except (TypeError, ValueError):
            entered_at = datetime.now(UTC)
        state = PositionState(
            sheet_id=sheet_id,
            ticker=ticker,
            entry_price=entry_price,
            quantity=quantity,
            entered_at=entered_at,
            high_watermark=entry_price,
            peak_return_pct=0.0,
        )
        await self._tracker.register(state)
        logger.info(
            "adopt_position registered sheet=%s ticker=%s entry=%.0f qty=%d by=%s",
            sheet_id,
            ticker,
            entry_price,
            quantity,
            command.issued_by,
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
            await self._execute_manual_sell(ticker, qty, source="telegram")

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
                if await self._execute_manual_sell(
                    p.stock_code, p.quantity, source="telegram_sellall"
                ):
                    count += 1
            logger.info("manual_sellall: sold %d/%d positions", count, len(state.positions))

    async def _execute_manual_sell(self, ticker: str, intended_qty: int, *, source: str) -> bool:
        """단일 manual_sell 실행 — KIS 매도 + v3 state 정합. 성공 시 True.

        v3 state (PositionTracker / executions / positions / ExitFilledEvent) 는
        보유 sheet 가 있을 때만 갱신. sheet 없는 종목 (외부 매수 잔재) 은 KIS
        호출만 + log only — audit D1 의 reconcile job 영역으로 분리.

        Notes:
            confirm_order 가 polling 으로 인해 수 초 block 가능. control consumer
            는 sequential 이라 이 동안 다른 명령 대기. manual_sellall 의 다종목
            매도 시 순차 처리 (병렬 X — KIS rate limit 보호).
        """
        from prime_jennie_runtime.kis_gateway.schemas import OrderRequest

        assert self._kis is not None  # apply() 단계에서 보장
        try:
            result = await self._kis.sell(
                OrderRequest(stock_code=ticker, quantity=intended_qty, order_type="market", price=0)
            )
        except Exception:
            logger.exception("manual_sell KIS submit failed ticker=%s qty=%d", ticker, intended_qty)
            return False

        if not result.success or not result.order_no:
            logger.warning(
                "manual_sell rejected ticker=%s qty=%d msg=%s",
                ticker,
                intended_qty,
                result.message,
            )
            return False

        # 체결 확인 — confirm_order 가 retry/interval 처리.
        try:
            status = await self._kis.confirm_order(
                result.order_no,
                max_retries=self._max_confirm_retries,
                interval=self._confirm_interval,
            )
        except Exception:
            logger.exception(
                "manual_sell confirm_order failed ticker=%s order_no=%s",
                ticker,
                result.order_no,
            )
            return False

        if status is None or status.filled_qty <= 0:
            logger.warning(
                "manual_sell not filled ticker=%s order_no=%s — left to user/KIS to resolve",
                ticker,
                result.order_no,
            )
            return False

        filled_qty = status.filled_qty
        filled_price = status.avg_price
        logger.info(
            "manual_sell filled ticker=%s qty=%d price=%.0f order_no=%s",
            ticker,
            filled_qty,
            filled_price,
            result.order_no,
        )

        # v3 state 정합 — 의존성 모두 주입된 경우만.
        await self._finalize_manual_sell(
            ticker=ticker,
            filled_qty=filled_qty,
            filled_price=filled_price,
            order_no=result.order_no,
            source=source,
        )
        return True

    async def _finalize_manual_sell(
        self,
        *,
        ticker: str,
        filled_qty: int,
        filled_price: float,
        order_no: str | None,
        source: str,
    ) -> None:
        """KIS 체결 후 v3 state 일관성 작업.

        sheet 가 없는 외부 보유 종목은 skip — reconcile job 이 별도 처리. 의존성
        하나라도 미주입이면 전체 skip (legacy 모드, KIS 만 호출).
        """
        if (
            self._tracker is None
            or self._recorder is None
            or self._sheet_fetcher is None
            or self._notifier is None
        ):
            logger.info(
                "manual_sell finalize skipped (deps missing) ticker=%s — KIS only",
                ticker,
            )
            return

        sheet_ids = self._tracker.sheet_ids_for(ticker)
        if not sheet_ids:
            logger.info(
                "manual_sell finalize: no v3 sheet for ticker=%s — KIS only (외부 보유)",
                ticker,
            )
            return

        sheet_id = sheet_ids[0]
        state = self._tracker._states.get(sheet_id)  # noqa: SLF001 — 내부 dict 직접 접근 (기존 패턴)
        if state is None:
            logger.warning(
                "manual_sell finalize: tracker index has sheet_id=%s but no state",
                sheet_id,
            )
            return

        now = datetime.now(UTC)
        fully_closed = filled_qty >= state.quantity

        # 1) PositionTracker — close 또는 quantity 감소 후 persist
        if fully_closed:
            await self._tracker.close(sheet_id)
        else:
            state.quantity -= filled_qty
            await self._tracker.persist(sheet_id)

        # 2) executions INSERT + positions UPDATE/DELETE
        sheets = await self._sheet_fetcher(sheet_id)
        if sheets:
            try:
                await self._recorder.record_sell(
                    sheets[0],
                    filled_qty=filled_qty,
                    filled_price=filled_price,
                    executed_at=now,
                    reason=f"manual_{source}",
                )
            except Exception:
                logger.exception(
                    "manual_sell record_sell failed sheet=%s — state already updated",
                    sheet_id,
                )

        # 3) Telegram 알림 — TradeNotification (P/L 포함)
        pnl_amount: float | None = None
        pnl_pct: float | None = None
        if state.entry_price > 0:
            pnl_amount = (filled_price - state.entry_price) * filled_qty
            pnl_pct = (filled_price - state.entry_price) / state.entry_price
        try:
            from prime_jennie_runtime.fast_loop.schemas import TradeNotification

            await self._notifier.emit(
                TradeNotification(
                    kind="exit_filled",
                    sheet_id=sheet_id,
                    ticker=ticker,
                    stock_name=ticker,
                    side="sell",
                    quantity=filled_qty,
                    price=filled_price,
                    ts=now,
                    reason=f"manual_{source}",
                    is_partial=not fully_closed,
                    entry_avg_price=state.entry_price if state.entry_price > 0 else None,
                    pnl_amount=pnl_amount,
                    pnl_pct=pnl_pct,
                )
            )
        except Exception:
            logger.exception("manual_sell TradeNotification emit failed sheet=%s", sheet_id)

        # 4) Coordinator Event Bus — ExitFilledEvent publish
        try:
            from prime_jennie_runtime.coordinator import (
                ExitFilledEvent,
                publish_event,
            )

            await publish_event(
                self._redis,
                ExitFilledEvent(
                    occurred_at=now,
                    correlation_id=sheet_id,
                    sheet_id=sheet_id,
                    ticker=ticker,
                    filled_qty=filled_qty,
                    filled_price=filled_price,
                    entry_price=state.entry_price,
                    reason=f"manual_{source}",
                    fully_closed=fully_closed,
                    pnl_amount=pnl_amount,
                    pnl_pct=pnl_pct,
                ),
            )
        except Exception:
            logger.exception("manual_sell publish_event(ExitFilled) failed sheet=%s", sheet_id)


# ---------------------------------------------------------------------
# 명령별 apply 함수 — async(redis, ControlCommand) → None
# ---------------------------------------------------------------------


async def _emergency_stop(redis: aioredis.Redis, cmd: ControlCommand) -> None:
    await redis.set(STATE_KEY_STOP, b"1")
    await redis.set(STATE_KEY_PAUSE, (cmd.reason or "emergency_stop").encode())
    await record_change(redis, "stop", cmd.issued_by, cmd.reason, cmd.issued_at)
    await record_change(redis, "pause", cmd.issued_by, cmd.reason, cmd.issued_at)


async def _pause(redis: aioredis.Redis, cmd: ControlCommand) -> None:
    await redis.set(STATE_KEY_PAUSE, (cmd.reason or "manual_pause").encode())
    await record_change(redis, "pause", cmd.issued_by, cmd.reason, cmd.issued_at)


async def _resume(redis: aioredis.Redis, cmd: ControlCommand) -> None:
    # v3 키 + v2 호환 키 모두 정리. v2 키는 운영자가 REAL_MODE_MIGRATION_CHECKLIST
    # 따라 수동 SET 하는 관행 — `_resume` 가 v3 키만 지우면 v2 호환 키가 남아
    # 그쪽 환경에 영향 (`SystemState.snapshot` 은 v3 키만 보지만, v2 운영 코드와
    # 공존 기간에 `trading_flags:stop=1` 잔존이 confusing). docs/CONTROL_STATE_KEYS.md
    # 참조. v2 deprecate 시 두 키는 import + 본 delete 호출 모두 제거.
    await redis.delete(STATE_KEY_STOP, STATE_KEY_PAUSE, V2_KEY_STOP, V2_KEY_PAUSE)
    await record_change(redis, "stop", cmd.issued_by, cmd.reason, cmd.issued_at)
    await record_change(redis, "pause", cmd.issued_by, cmd.reason, cmd.issued_at)


async def _set_dryrun(redis: aioredis.Redis, cmd: ControlCommand) -> None:
    enabled = bool(cmd.payload.get("enabled", False))
    if enabled:
        await redis.set(STATE_KEY_DRYRUN, b"1")
    else:
        await redis.delete(STATE_KEY_DRYRUN)
    await record_change(redis, "dryrun", cmd.issued_by, cmd.reason, cmd.issued_at)


async def _liquidate_arm(redis: aioredis.Redis, cmd: ControlCommand) -> None:
    await redis.set(STATE_KEY_LIQUIDATE_ARMED, b"1")
    await record_change(redis, "liquidate_armed", cmd.issued_by, cmd.reason, cmd.issued_at)


async def _liquidate_disarm(redis: aioredis.Redis, cmd: ControlCommand) -> None:
    await redis.delete(STATE_KEY_LIQUIDATE_ARMED)
    await record_change(redis, "liquidate_armed", cmd.issued_by, cmd.reason, cmd.issued_at)


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
