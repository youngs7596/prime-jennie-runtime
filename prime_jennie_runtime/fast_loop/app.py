"""Fast Loop long-running runner (entrypoint for `fast-loop` 컨테이너).

- `PositionSheetConsumer` — Slow loop 가 발행한 시트를 읽어 entry 실행
- `TickLoop` — KIS Gateway 가 발행한 가격 이벤트로 exit 평가
- `SystemState` — `control.state:*` 을 읽어 stop/pause 동안 신규 진입을 차단

기동 시 `PositionTracker.load_from_redis()` 로 활성 포지션 복원. 주문 경로는
`trading_flags:stop` 또는 control.state:stop/pause 가 True 면 자동으로 막힌다.

실행:
    python -m prime_jennie_runtime.fast_loop.app
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import AsyncExitStack

import asyncpg
import redis.asyncio as aioredis

from prime_jennie_runtime.control.state import SystemState
from prime_jennie_runtime.fast_loop.consumer import PositionSheetConsumer
from prime_jennie_runtime.fast_loop.entry_executor import EntryExecutor
from prime_jennie_runtime.fast_loop.exit_executor import ExitExecutor
from prime_jennie_runtime.fast_loop.gateway_subscriber import subscribe_on_startup
from prime_jennie_runtime.fast_loop.kis_client import KisClient
from prime_jennie_runtime.fast_loop.notifier import Notifier
from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker
from prime_jennie_runtime.fast_loop.tick_loop import TickLoop
from prime_jennie_runtime.infra.config import AppConfig
from prime_jennie_runtime.position_sheet.schema import PositionSheet

logger = logging.getLogger(__name__)


class PostgresSheetFetcher:
    """`position_sheets` 테이블 에서 sheet_id → PositionSheet 조회 (TickLoop 용)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def __call__(self, sheet_id: str) -> list[PositionSheet]:
        row = await self._pool.fetchrow(
            "SELECT sheet_json FROM position_sheets WHERE sheet_id = $1", sheet_id
        )
        if row is None:
            return []
        try:
            return [PositionSheet.model_validate(row["sheet_json"])]
        except Exception:
            logger.exception("invalid sheet_json in DB: sheet_id=%s", sheet_id)
            return []


class BalanceAwareSizer:
    """계좌 잔고 × final_pct / current_price → 정수 수량.

    stop/pause 상태면 0 반환 → PositionSheetConsumer 가 entry skip.
    ``cache_ttl_sec`` 동안 잔고/시세를 캐시하여 연쇄 시트 처리 시 rate limit 회피.
    """

    def __init__(
        self,
        kis: KisClient,
        system_state: SystemState,
        *,
        cache_ttl_sec: float = 5.0,
    ) -> None:
        self._kis = kis
        self._system = system_state
        self._ttl = cache_ttl_sec
        self._cached_balance: tuple[float, int] | None = None  # (expires_at, krw)

    async def __call__(self, sheet: PositionSheet) -> int:
        snapshot = await self._system.snapshot()
        if not snapshot.entry_allowed:
            logger.info(
                "entry blocked by control state: sheet=%s stopped=%s paused=%s",
                sheet.sheet_id,
                snapshot.stopped,
                snapshot.paused,
            )
            return 0

        balance_krw = await self._get_cached_balance()
        try:
            stock = await self._kis.get_snapshot(sheet.ticker)
        except Exception:
            logger.exception("snapshot failed ticker=%s", sheet.ticker)
            return 0
        if stock.price <= 0:
            return 0

        notional = int(balance_krw * sheet.size.final_pct)
        qty = notional // int(stock.price)
        return max(qty, 0)

    async def _get_cached_balance(self) -> int:
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._cached_balance is not None and now < self._cached_balance[0]:
            return self._cached_balance[1]
        portfolio = await self._kis.get_balance()
        krw = int(portfolio.cash_balance)
        self._cached_balance = (now + self._ttl, krw)
        return krw


async def run() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = AppConfig()

    async with AsyncExitStack() as stack:
        redis_client = aioredis.from_url(cfg.redis.url, decode_responses=False)
        stack.push_async_callback(redis_client.aclose)

        from prime_jennie_runtime.infra.heartbeat import HeartbeatPublisher

        heartbeat = HeartbeatPublisher(redis_client, service="fast-loop")
        await heartbeat.start()
        stack.push_async_callback(heartbeat.stop)

        pool = await asyncpg.create_pool(
            host=cfg.postgres.host,
            port=cfg.postgres.port,
            user=cfg.postgres.user,
            password=cfg.postgres.password,
            database=cfg.postgres.db,
            min_size=1,
            max_size=4,
        )
        stack.push_async_callback(pool.close)

        kis = KisClient(cfg.kis)
        stack.push_async_callback(kis.close)

        tracker = PositionTracker(redis_client)
        restored = await tracker.load_from_redis()
        logger.info("position tracker restored %d active positions", restored)

        notifier = Notifier(redis_client)
        system_state = SystemState(redis_client)

        entry_executor = EntryExecutor(kis=kis, tracker=tracker, notifier=notifier)
        exit_executor = ExitExecutor(kis=kis, tracker=tracker, notifier=notifier)

        sheet_fetcher = PostgresSheetFetcher(pool)

        sheet_consumer = PositionSheetConsumer(
            redis_client=redis_client,
            entry_executor=entry_executor,
            account_sizer=BalanceAwareSizer(kis, system_state),
            kis=kis,
        )
        await sheet_consumer.ensure_group()

        tick_loop = TickLoop(
            redis_client=redis_client,
            tracker=tracker,
            exit_executor=exit_executor,
            sheet_fetcher=sheet_fetcher,
        )
        await tick_loop.ensure_group()

        # gateway 에 실시간 체결가 구독 요청 (v2 monitor/scanner 포팅).
        # 실패해도 tick_loop 은 계속 구동 — price-scheduler 폴링이 fallback.
        await subscribe_on_startup(pool, cfg.kis.gateway_url)

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _request_stop() -> None:
            stop_event.set()
            sheet_consumer.stop()
            tick_loop.stop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _request_stop)

        logger.info("fast_loop runner ready — starting sheet consumer + tick loop")
        consumer_task = asyncio.create_task(sheet_consumer.run(), name="sheet-consumer")
        tick_task = asyncio.create_task(tick_loop.run(), name="tick-loop")

        done, pending = await asyncio.wait(
            {consumer_task, tick_task, asyncio.create_task(stop_event.wait())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        for t in done:
            if t.cancelled():
                continue
            if (exc := t.exception()) is not None:
                logger.error("fast_loop task exited with error: %s", exc)

        logger.info("fast_loop runner shutting down")


if __name__ == "__main__":
    asyncio.run(run())
