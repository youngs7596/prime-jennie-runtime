"""Position Sheet Consumer — v3:position_sheets Stream 구독.

Slow loop가 발행한 시트를 소비하여 entry_executor에 전달.

수량 계산은 호출자가 제공하는 `account_sizer`에 위임 (계좌 잔고 * final_pct / current_price).
Phase 1은 단순 stub으로 시작 가능.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import redis.asyncio as aioredis

from prime_jennie_runtime.fast_loop.entry_executor import EntryExecutor, EntryOutcome
from prime_jennie_runtime.fast_loop.kis_client import KisClient
from prime_jennie_runtime.infra.redis_streams import (
    STREAM_POSITION_SHEETS,
    TypedStreamConsumer,
)
from prime_jennie_runtime.position_sheet.schema import PositionSheet

logger = logging.getLogger(__name__)

AccountSizer = Callable[[PositionSheet], Awaitable[int]]


class PositionSheetConsumer:
    """v3:position_sheets → entry_executor 호출."""

    def __init__(
        self,
        redis_client: aioredis.Redis,
        entry_executor: EntryExecutor,
        account_sizer: AccountSizer,
        kis: KisClient,
        *,
        group: str = "fast_loop",
        consumer: str = "fast_loop_1",
        subscribe_on_entry: bool = True,
    ):
        self._redis = redis_client
        self._entry = entry_executor
        self._sizer = account_sizer
        self._kis = kis
        self._subscribe_on_entry = subscribe_on_entry
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
        try:
            qty = await self._sizer(sheet)
        except Exception:
            logger.exception("account_sizer failed sheet=%s", sheet.sheet_id)
            return

        if qty <= 0:
            logger.info("entry skipped qty<=0 sheet=%s", sheet.sheet_id)
            return

        outcome: EntryOutcome = await self._entry.execute(sheet, quantity=qty)
        if outcome.success and self._subscribe_on_entry:
            try:
                await self._kis.subscribe([sheet.ticker])
            except Exception:
                logger.exception("subscribe failed ticker=%s", sheet.ticker)
