"""Tick Loop — kis:prices Stream 소비 → 활성 포지션마다 exit 평가.

KIS Gateway가 WebSocket에서 받은 가격 이벤트를 STREAM_PRICES에 발행하면,
이 루프가 consumer group으로 읽고 ticker에 해당하는 active sheet를 찾아
exit_evaluator로 평가. 매칭된 ExitDecision은 exit_executor로 실행.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

import redis.asyncio as aioredis

from prime_jennie_runtime.fast_loop.domain import TickData
from prime_jennie_runtime.fast_loop.exit_evaluator import evaluate as evaluate_exit
from prime_jennie_runtime.fast_loop.exit_executor import ExitExecutor
from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker
from prime_jennie_runtime.infra.redis_streams import STREAM_PRICES
from prime_jennie_runtime.position_sheet.schema import PositionSheet

logger = logging.getLogger(__name__)

# 시트 조회 콜백 — sheet_id → 해당 시트. 캐시 구현은 호출자 책임.
SheetFetcher = Callable[[str], Awaitable[list[PositionSheet]]]


class TickLoop:
    """STREAM_PRICES 소비. 한 tick당 ticker의 모든 active sheet 평가."""

    def __init__(
        self,
        redis_client: aioredis.Redis,
        tracker: PositionTracker,
        exit_executor: ExitExecutor,
        sheet_fetcher: SheetFetcher,
        *,
        group: str = "fast_loop_ticks",
        consumer: str = "fast_loop_tick_1",
        stream: str = STREAM_PRICES,
        block_ms: int = 1000,
    ):
        self._redis = redis_client
        self._tracker = tracker
        self._exit_executor = exit_executor
        self._sheet_fetcher = sheet_fetcher
        self._group = group
        self._consumer = consumer
        self._stream = stream
        self._block_ms = block_ms
        self._running = False

    async def ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(self._stream, self._group, id="0", mkstream=True)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def run(self) -> None:
        self._running = True
        await self.ensure_group()
        while self._running:
            try:
                messages = await self._redis.xreadgroup(
                    self._group,
                    self._consumer,
                    {self._stream: ">"},
                    count=10,
                    block=self._block_ms,
                )
            except aioredis.ConnectionError:
                logger.exception("redis connection lost in tick_loop")
                break
            if not messages:
                continue
            for _stream, entries in messages:
                for msg_id, data in entries:
                    await self._process(msg_id, data)

    def stop(self) -> None:
        self._running = False

    async def process_one(self, msg_id: bytes | str, data: dict) -> None:
        """테스트용 — 메시지 하나 수동 처리."""
        await self._process(msg_id, data)

    async def _process(self, msg_id: bytes | str, data: dict) -> None:
        mid = msg_id.decode() if isinstance(msg_id, bytes) else msg_id
        await self._redis.xack(self._stream, self._group, mid)

        tick = _parse_tick(data)
        if tick is None:
            return

        sheet_ids = self._tracker.sheet_ids_for(tick.ticker)
        if not sheet_ids:
            return

        for sheet_id in sheet_ids:
            state = self._tracker.get(sheet_id)
            if state is None:
                continue
            sheets = await self._sheet_fetcher(sheet_id)
            if not sheets:
                continue
            sheet = sheets[0]
            decision = evaluate_exit(sheet, state, tick)
            if decision is None:
                await self._tracker.persist(sheet_id)  # state가 변경됐을 수 있음 (high_watermark)
                continue

            await self._exit_executor.execute(state, decision)


def _parse_tick(data: dict) -> TickData | None:
    """XADD payload → TickData. `kis_gateway.streamer`가 발행하는 포맷."""
    raw_payload = data.get(b"payload") or data.get("payload")
    if raw_payload is None:
        # 일부 streamer는 개별 필드로 발행할 수 있음
        try:
            ticker = _decode(data, "ticker") or _decode(data, "stock_code")
            price = _decode(data, "price")
            ts = _decode(data, "ts")
            if ticker is None or price is None:
                return None
            return TickData(
                ticker=ticker,
                price=float(price),
                ts=_parse_ts(ts),
            )
        except Exception:
            logger.exception("tick parse failed")
            return None

    if isinstance(raw_payload, bytes):
        raw_payload = raw_payload.decode()
    try:
        obj = json.loads(raw_payload)
    except json.JSONDecodeError:
        logger.warning("tick payload not json")
        return None
    return TickData(
        ticker=str(obj.get("ticker") or obj.get("stock_code")),
        price=float(obj["price"]),
        ts=_parse_ts(obj.get("ts")),
        rsi_1m=obj.get("rsi_1m"),
        volume=obj.get("volume"),
    )


def _decode(data: dict, key: str) -> str | None:
    val = data.get(key) or data.get(key.encode())
    if val is None:
        return None
    if isinstance(val, bytes):
        return val.decode()
    return str(val)


def _parse_ts(raw: str | None) -> datetime:
    if raw is None:
        from datetime import UTC

        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        from datetime import UTC

        return datetime.now(UTC)
