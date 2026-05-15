"""Fast Loop long-running runner (entrypoint for `fast-loop` 컨테이너).

- `PositionSheetConsumer` — Slow loop 가 발행한 시트를 읽어 entry 실행
- `TickLoop` — KIS Gateway 가 발행한 가격 이벤트로 exit 평가
- `SystemState` — `control.state:*` 을 읽어 stop/pause 동안 신규 진입을 차단

기동 시 `PositionTracker.load_from_redis()` 로 활성 포지션 복원. 주문 경로는
`control.state:stop` / `control.state:pause` 가 True 면 자동으로 막힌다.

v2 호환 키 (`trading_flags:stop` 등) 는 fast_loop 본체에서 더 이상 읽지
않는다. 운영자가 REAL_MODE_MIGRATION_CHECKLIST 따라 수동 SET 하는 잔존
관행만 남아 있으며 `/resume` 명령이 ControlCommandConsumer 경유로 함께
정리한다. 자세한 매핑은 docs/CONTROL_STATE_KEYS.md.

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
import httpx
import redis.asyncio as aioredis

from prime_jennie_runtime.control.consumer import ControlCommandConsumer
from prime_jennie_runtime.control.state import SystemState
from prime_jennie_runtime.fast_loop.bar_engine import BarEngine, BarEngineIndicatorProvider
from prime_jennie_runtime.fast_loop.consumer import PositionSheetConsumer
from prime_jennie_runtime.fast_loop.cooldown_check import PgCooldownChecker
from prime_jennie_runtime.fast_loop.entry_executor import EntryExecutor
from prime_jennie_runtime.fast_loop.exit_executor import ExitExecutor
from prime_jennie_runtime.fast_loop.gateway_subscriber import subscribe_on_startup
from prime_jennie_runtime.fast_loop.kis_client import KisClient
from prime_jennie_runtime.fast_loop.market_snapshot import RuntimeMarketSnapshotFetcher
from prime_jennie_runtime.fast_loop.notifier import Notifier
from prime_jennie_runtime.fast_loop.pending_entry import (
    EntryConditionEvaluator,
    PendingEntryQueue,
)
from prime_jennie_runtime.fast_loop.persistence import (
    PostgresStockNameResolver,
    PostgresTradeRecorder,
)
from prime_jennie_runtime.fast_loop.position_sync_check import check_state_kis_mismatch
from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker
from prime_jennie_runtime.fast_loop.risk_throttle import IntradayRiskThrottle
from prime_jennie_runtime.fast_loop.risk_updater import run_risk_updater
from prime_jennie_runtime.fast_loop.schemas import RiskLevelChangeNotification
from prime_jennie_runtime.fast_loop.tick_loop import TickLoop
from prime_jennie_runtime.infra.config import AppConfig, validate_kis_gateway_url
from prime_jennie_runtime.infra.redis_streams import STREAM_NOTIFICATIONS, TypedStreamPublisher
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
        raw = row["sheet_json"]
        try:
            if isinstance(raw, (str, bytes, bytearray)):
                return [PositionSheet.model_validate_json(raw)]
            return [PositionSheet.model_validate(raw)]
        except Exception:
            logger.exception("invalid sheet_json in DB: sheet_id=%s", sheet_id)
            return []


class BalanceAwareSizer:
    """총자산 × final_pct × intraday_mult / current_price → 정수 수량.

    분모는 ``total_asset`` (cash + stock_eval) — v2 의 sizing 분모와 동일.
    cash_balance 만 쓰면 보유 종목 다수 보유 시 cash 가 잠겨 신규 슬롯이
    사실상 미니 사이즈가 되는 문제 회피.

    notional 캡 (모두 적용 후 min):
    - total_asset × sheet.size.max_notional_pct — 자산비례 cap (v2 동등, 12%)
    - sheet.size.max_notional_krw — 절대 안전 한도
    - cash_balance — 가용 현금. 초과 시 KIS 가 거부하므로 사전 클램프.

    stop/pause 상태면 0 반환 → PositionSheetConsumer 가 entry skip.
    ``cache_ttl_sec`` 동안 잔고/시세를 캐시하여 연쇄 시트 처리 시 rate limit 회피.

    ``risk_throttle`` 주입 시 가장 최근 intraday level 의 multiplier 를 추가 적용.
    slow-loop 가 NoOpRiskThrottle 인 동안만 의미 있는 보강 — 추후 slow-loop 가
    동일 throttle 을 읽도록 통합되면 중복 적용을 피하기 위해 제거 검토.
    """

    def __init__(
        self,
        kis: KisClient,
        system_state: SystemState,
        *,
        cache_ttl_sec: float = 5.0,
        risk_throttle: IntradayRiskThrottle | None = None,
    ) -> None:
        self._kis = kis
        self._system = system_state
        self._ttl = cache_ttl_sec
        self._risk_throttle = risk_throttle
        # (expires_at, total_asset, cash_balance)
        self._cached_balance: tuple[float, int, int] | None = None

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

        total_asset, cash_balance = await self._get_cached_balance()
        try:
            stock = await self._kis.get_snapshot(sheet.ticker)
        except Exception:
            logger.exception("snapshot failed ticker=%s", sheet.ticker)
            return 0
        if stock.price <= 0:
            return 0

        intraday_mult = (
            self._risk_throttle.current_multiplier() if self._risk_throttle is not None else 1.0
        )
        target_notional = int(total_asset * sheet.size.final_pct * intraday_mult)
        caps = [target_notional, sheet.size.max_notional_krw, cash_balance]
        if sheet.size.max_notional_pct is not None:
            caps.append(int(total_asset * sheet.size.max_notional_pct))
        notional = min(caps)
        qty = notional // int(stock.price)
        return max(qty, 0)

    async def _get_cached_balance(self) -> tuple[int, int]:
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._cached_balance is not None and now < self._cached_balance[0]:
            return self._cached_balance[1], self._cached_balance[2]
        portfolio = await self._kis.get_balance()
        total = int(portfolio.total_asset)
        cash = int(portfolio.cash_balance)
        self._cached_balance = (now + self._ttl, total, cash)
        return total, cash


async def run() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = AppConfig()

    # KIS_GATEWAY_URL 누락/localhost 시 production 에선 startup 차단, 그 외 dev 는
    # docker-compose 기본값으로 fallback (2026-04 자산 스냅샷 사고 학습).
    resolved_gw_url = validate_kis_gateway_url(cfg.kis, strict=cfg.env == "production")
    cfg.kis.gateway_url = resolved_gw_url

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

        http_client = httpx.AsyncClient(timeout=15.0)
        stack.push_async_callback(http_client.aclose)

        tracker = PositionTracker(redis_client)
        restored = await tracker.load_from_redis()
        logger.info("position tracker restored %d active positions", restored)

        pending_queue = PendingEntryQueue(redis_client)
        pending_restored = await pending_queue.load_from_redis()
        logger.info("pending entry queue restored %d sheets from redis", pending_restored)
        # 1분봉 indicator 인프라 — tick_loop 가 매 tick 마다 update 호출 →
        # ma20 / RSI / recent_high lookup 을 EntryConditionEvaluator 에 제공.
        bar_engine = BarEngine()
        indicators = BarEngineIndicatorProvider(bar_engine)
        entry_evaluator = EntryConditionEvaluator(indicators=indicators)

        notifier = Notifier(redis_client)
        system_state = SystemState(redis_client)
        recorder = PostgresTradeRecorder(pool)
        stock_resolver = PostgresStockNameResolver(pool)

        # 부팅 시 KIS 실잔고 vs in-memory state mismatch 검사 — 외부 청산으로
        # 인한 stale state 즉시 인지 (자동 정리는 하지 않음, 사용자 결정 보존).
        await check_state_kis_mismatch(tracker, kis, notifier)

        control_consumer = ControlCommandConsumer(
            redis_client, consumer_name=f"control-fast-{cfg.env}", kis_client=kis
        )

        risk_throttle = IntradayRiskThrottle(redis_client)
        await risk_throttle.load_state()
        snapshot_fetcher = RuntimeMarketSnapshotFetcher(
            http=http_client, redis_client=redis_client, pool=pool
        )
        risk_publisher = TypedStreamPublisher(
            redis_client, STREAM_NOTIFICATIONS, RiskLevelChangeNotification
        )

        sheet_fetcher = PostgresSheetFetcher(pool)

        entry_executor = EntryExecutor(
            kis=kis,
            tracker=tracker,
            notifier=notifier,
            recorder=recorder,
            system_state=system_state,
            stock_resolver=stock_resolver,
        )
        exit_executor = ExitExecutor(
            kis=kis,
            tracker=tracker,
            notifier=notifier,
            recorder=recorder,
            sheet_fetcher=sheet_fetcher,
            system_state=system_state,
            stock_resolver=stock_resolver,
        )
        sizer = BalanceAwareSizer(kis, system_state, risk_throttle=risk_throttle)

        sheet_consumer = PositionSheetConsumer(
            redis_client=redis_client,
            pending_queue=pending_queue,
            tracker=tracker,
            kis=kis,
            cooldown_checker=PgCooldownChecker(pool),
        )
        await sheet_consumer.ensure_group()

        tick_loop = TickLoop(
            redis_client=redis_client,
            tracker=tracker,
            exit_executor=exit_executor,
            sheet_fetcher=sheet_fetcher,
            pending_queue=pending_queue,
            entry_evaluator=entry_evaluator,
            entry_executor=entry_executor,
            account_sizer=sizer,
            bar_engine=bar_engine,
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
            control_consumer.stop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _request_stop)

        logger.info(
            "fast_loop runner ready — starting sheet consumer + tick loop + purge loop"
            " + control consumer + risk updater"
        )
        consumer_task = asyncio.create_task(sheet_consumer.run(), name="sheet-consumer")
        tick_task = asyncio.create_task(tick_loop.run(), name="tick-loop")
        purge_task = asyncio.create_task(
            pending_queue.purge_loop(interval_sec=60.0), name="pending-purge"
        )
        control_task = asyncio.create_task(control_consumer.run(), name="control-consumer")
        risk_task = asyncio.create_task(
            run_risk_updater(
                throttle=risk_throttle,
                fetch_snapshot=snapshot_fetcher,
                interval_sec=300,
                notifier=risk_publisher,
            ),
            name="risk-updater",
        )

        done, pending = await asyncio.wait(
            {
                consumer_task,
                tick_task,
                purge_task,
                control_task,
                risk_task,
                asyncio.create_task(stop_event.wait()),
            },
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
