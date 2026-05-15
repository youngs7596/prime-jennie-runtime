"""Position Sheet Consumer — v3:position_sheets Stream 구독.

Slow loop 가 발행한 시트를 소비하여 ``PendingEntryQueue`` 에 적재한다.
실제 매수는 ``TickLoop`` 가 매 tick 마다 conditions 를 재평가하여 만족 시점에
``EntryExecutor`` 를 호출한다 (POSITION_SHEET_SPEC §4.1 / §4.3).

Dedup 가드:
    - 같은 ticker 가 이미 ``PositionTracker`` 에 보유 중이면 reject
    - 같은 ticker 의 시트가 이미 큐에 있으면 reject
    - 시트가 큐에 적재되면 KIS streamer 에 ticker subscribe 요청 (tick 수신 보장)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import redis.asyncio as aioredis

from prime_jennie_runtime.fast_loop.cooldown_check import (
    CooldownChecker,
    NullCooldownChecker,
)
from prime_jennie_runtime.fast_loop.kis_client import KisClient
from prime_jennie_runtime.fast_loop.pending_entry import PendingEntryQueue
from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker
from prime_jennie_runtime.infra.redis_streams import (
    STREAM_POSITION_SHEETS,
    TypedStreamConsumer,
)
from prime_jennie_runtime.position_sheet.schema import PositionSheet

logger = logging.getLogger(__name__)


class PositionSheetConsumer:
    """v3:position_sheets → PendingEntryQueue 적재."""

    def __init__(
        self,
        redis_client: aioredis.Redis,
        pending_queue: PendingEntryQueue,
        tracker: PositionTracker,
        kis: KisClient,
        *,
        group: str = "fast_loop",
        consumer: str = "fast_loop_1",
        subscribe_on_enqueue: bool = True,
        cooldown_checker: CooldownChecker | None = None,
    ):
        self._redis = redis_client
        self._queue = pending_queue
        self._tracker = tracker
        self._kis = kis
        self._subscribe_on_enqueue = subscribe_on_enqueue
        self._cooldown = cooldown_checker or NullCooldownChecker()
        self._consumer = TypedStreamConsumer(
            redis_client,
            STREAM_POSITION_SHEETS,
            group,
            consumer,
            PositionSheet,
            self._handle,
            batch_size=1,
            block_ms=1000,
        )

    async def ensure_group(self) -> None:
        await self._consumer.ensure_group()

    async def run(self) -> None:
        await self._consumer.run()

    def stop(self) -> None:
        self._consumer.stop()

    async def _handle(self, sheet: PositionSheet) -> None:
        # 1) ticker dedup — 보유 중이면 reject
        if self._tracker.sheet_ids_for(sheet.ticker):
            logger.info(
                "sheet rejected (already holding): ticker=%s sheet=%s",
                sheet.ticker,
                sheet.sheet_id,
            )
            await self._emit_entry_rejected(sheet, reason="already_holding")
            return

        # 2) ticker dedup — 이미 큐에 같은 ticker 시트 있으면 reject
        if self._queue.contains_ticker(sheet.ticker):
            logger.info(
                "sheet rejected (ticker already pending): ticker=%s sheet=%s",
                sheet.ticker,
                sheet.sheet_id,
            )
            await self._emit_entry_rejected(sheet, reason="ticker_pending")
            return

        # 3) recent stop-loss cooldown — 24h 내 같은 종목 손절 이력 있으면 reject.
        # design: .ai/designs/2026-05-15-cooldown-and-duplicate-guard.md §3
        try:
            recent = await self._cooldown.has_recent_stoploss(sheet.ticker)
        except Exception:
            logger.exception(
                "cooldown check failed (fail-open) ticker=%s sheet=%s",
                sheet.ticker,
                sheet.sheet_id,
            )
            recent = None
        if recent is not None:
            logger.info(
                "sheet rejected (recent_stoploss_cooldown): "
                "ticker=%s sheet=%s last_exit=%s reason=%s",
                sheet.ticker,
                sheet.sheet_id,
                recent.last_exit_at.isoformat(),
                recent.reason,
            )
            await self._emit_entry_rejected(sheet, reason="recent_stoploss_cooldown")
            return

        # 4) 큐 적재 — 같은 sheet_id 가 이미 있으면 noop
        added = await self._queue.add(sheet)
        if not added:
            logger.info(
                "sheet rejected (sheet_id already in queue): sheet=%s",
                sheet.sheet_id,
            )
            await self._emit_entry_rejected(sheet, reason="sheet_id_in_queue")
            return

        logger.info(
            "sheet enqueued for entry condition evaluation: ticker=%s sheet=%s "
            "trigger=%s n_conditions=%d valid_until=%s",
            sheet.ticker,
            sheet.sheet_id,
            sheet.entry.trigger,
            len(sheet.entry.conditions),
            sheet.entry.valid_until.isoformat(),
        )

        # Coordinator Event Bus hook (Stage 1, best-effort).
        # 큐 적재 성공 직후 발행 — 실패해도 매매 path 영향 X.
        try:
            from prime_jennie_runtime.coordinator import (
                EntryQueuedEvent,
                publish_event,
            )

            await publish_event(
                self._redis,
                EntryQueuedEvent(
                    occurred_at=datetime.now(UTC),
                    correlation_id=sheet.sheet_id,
                    sheet_id=sheet.sheet_id,
                    ticker=sheet.ticker,
                    queue_size_after=self._queue.size(),
                ),
            )
        except Exception:
            logger.exception(
                "coordinator publish_event(EntryQueued) failed sheet=%s", sheet.sheet_id
            )

        # 5) tick 수신을 위해 KIS streamer 에 ticker subscribe
        if self._subscribe_on_enqueue:
            try:
                await self._kis.subscribe([sheet.ticker])
            except Exception:
                logger.exception("subscribe failed ticker=%s", sheet.ticker)

    async def _emit_entry_rejected(self, sheet: PositionSheet, *, reason: str) -> None:
        """Coordinator Event Bus hook — consumer dedup reject 시 (best-effort).

        Stage 1 관찰 전용. 매매 path 는 이미 return 으로 끝났으므로 영향 X.
        """
        try:
            from prime_jennie_runtime.coordinator import (
                EntryRejectedEvent,
                publish_event,
            )

            await publish_event(
                self._redis,
                EntryRejectedEvent(
                    occurred_at=datetime.now(UTC),
                    correlation_id=sheet.sheet_id,
                    sheet_id=sheet.sheet_id,
                    ticker=sheet.ticker,
                    reason=reason,
                ),
            )
        except Exception:
            logger.exception(
                "coordinator publish_event(EntryRejected) failed sheet=%s reason=%s",
                sheet.sheet_id,
                reason,
            )
