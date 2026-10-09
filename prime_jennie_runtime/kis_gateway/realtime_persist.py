"""실시간 체결·호가 적재 버퍼 (게이트웨이 안 background flush).

스트리머의 웹소켓 메시지 루프는 빨라야 한다 (KIS PINGPONG 응답이 늦으면 연결이
끊긴다). 그래서 틱마다 DB 를 치지 않고, 파싱한 레코드를 메모리 버퍼에 쌓아두고
별도 비동기 태스크가 주기적으로 묶음 INSERT 한다.

- `add_tick` / `add_orderbook`: 스트리머가 호출하는 동기 append (논블로킹).
- `_flush_loop`: `flush_interval` 마다 버퍼를 비워 `copy_records_to_table` 로 적재.
- `max_buffer` 초과 시 드롭하고 카운트 — flush 가 밀려도 게이트웨이 메모리를 지킨다.

설계: .ai/designs/2026-06-16-realtime-orderbook-capture.md
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_TICK_TABLE = "realtime_ticks"
_TICK_COLUMNS = (
    "stock_code",
    "ts",
    "price",
    "volume",
    "cum_volume",
    "trade_sign",
    "strength",
    "best_ask",
    "best_bid",
    "after_close",
)

_OB_TABLE = "realtime_orderbook"
_OB_COLUMNS = (
    "stock_code",
    "ts",
    "ask_prices",
    "ask_volumes",
    "bid_prices",
    "bid_volumes",
    "total_ask",
    "total_bid",
    "after_close",
)

_KST = timezone(timedelta(hours=9))
# 이 시각 이후 행은 장 마감 뒤로 표시한다 (migration 035 와 같은 규칙).
_AFTER_CLOSE_FROM = time(15, 31)


def _after_close(ts: datetime) -> bool | None:
    """KST 15:31 이후면 TRUE, 정규장은 NULL. 시각 칸(ts)은 레코드 두 번째 값."""
    return True if ts.astimezone(_KST).time() >= _AFTER_CLOSE_FROM else None


class RealtimeBuffer:
    """체결·호가 레코드를 모았다가 묶음으로 적재하는 버퍼."""

    def __init__(
        self,
        pool: Any,
        *,
        flush_interval: float = 1.5,
        max_buffer: int = 50_000,
    ):
        self._pool = pool
        self._flush_interval = flush_interval
        self._max_buffer = max_buffer

        self._ticks: list[tuple] = []
        self._orderbook: list[tuple] = []
        self._task: asyncio.Task | None = None
        self._running = False
        self._dropped = 0

    def add_tick(self, record: tuple) -> None:
        """체결 레코드 추가 (_TICK_COLUMNS 순서, after_close 제외). 버퍼 초과 시 드롭."""
        if self._buffered >= self._max_buffer:
            self._dropped += 1
            return
        self._ticks.append(record)

    def add_orderbook(self, record: tuple) -> None:
        """호가 레코드 추가 (_OB_COLUMNS 순서, after_close 제외). 버퍼 초과 시 드롭."""
        if self._buffered >= self._max_buffer:
            self._dropped += 1
            return
        self._orderbook.append(record)

    @property
    def _buffered(self) -> int:
        return len(self._ticks) + len(self._orderbook)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._flush_loop())
        logger.info("RealtimeBuffer started (interval=%.1fs)", self._flush_interval)

    async def stop(self) -> None:
        """flush 루프 종료 + 남은 버퍼를 마지막으로 비운다."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        with contextlib.suppress(Exception):
            await self.flush()
        logger.info("RealtimeBuffer stopped (dropped=%d)", self._dropped)

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._flush_interval)
            try:
                await self.flush()
            except Exception as e:
                # flush 실패가 루프를 죽이지 않도록 — 다음 주기에 재시도.
                logger.warning("realtime flush failed: %s", e)

    async def flush(self) -> None:
        """버퍼를 스냅샷해 비우고 묶음 적재. await 사이에 append 와 안 겹치게 먼저 swap."""
        if not self._ticks and not self._orderbook:
            return
        ticks, self._ticks = self._ticks, []
        orderbook, self._orderbook = self._orderbook, []

        # 스트리머가 만든 레코드 끝에 장 마감 뒤 표시를 붙인다 (적재 칸 순서 맞춤).
        ticks = [(*r, _after_close(r[1])) for r in ticks]
        orderbook = [(*r, _after_close(r[1])) for r in orderbook]

        async with self._pool.acquire() as conn:
            if ticks:
                await conn.copy_records_to_table(_TICK_TABLE, records=ticks, columns=_TICK_COLUMNS)
            if orderbook:
                await conn.copy_records_to_table(_OB_TABLE, records=orderbook, columns=_OB_COLUMNS)
        logger.debug("realtime flush: ticks=%d orderbook=%d", len(ticks), len(orderbook))


__all__ = ["RealtimeBuffer"]
