"""Tick Loop — kis:prices Stream 소비.

매 tick 당 다음 두 path 를 평가:

1. ``exit_evaluator`` — 보유 중 (``PositionTracker``) 인 ticker 의 active sheet 들에
   대해 9종 exit rule 평가. 매칭 시 ``ExitExecutor`` 로 매도.
2. ``entry_condition_evaluator`` — ``PendingEntryQueue`` 에 적재된 같은 ticker 의
   시트들에 대해 ``EntryCondition`` (price_below/above 등) 재평가. 통과 시
   ``account_sizer`` 로 수량 계산 후 ``EntryExecutor`` 로 매수. 성공이면 큐에서
   제거. 미체결/실패 시 큐 유지 → 다음 tick 재시도.

POSITION_SHEET_SPEC §4.1 / §4.3 의 "valid_until 내 재평가" 의미가 여기 구현됨.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta

import redis.asyncio as aioredis

from prime_jennie_runtime.fast_loop.bar_engine import BarEngine
from prime_jennie_runtime.fast_loop.domain import ExitDecision, TickData
from prime_jennie_runtime.fast_loop.entry_executor import EntryExecutor
from prime_jennie_runtime.fast_loop.exit_evaluator import evaluate as evaluate_exit
from prime_jennie_runtime.fast_loop.exit_executor import ExitExecutor
from prime_jennie_runtime.fast_loop.pending_entry import (
    EntryConditionEvaluator,
    PendingEntryQueue,
)
from prime_jennie_runtime.fast_loop.position_tracker import PositionTracker
from prime_jennie_runtime.infra.redis_streams import STREAM_PRICES
from prime_jennie_runtime.position_sheet.schema import KST, PositionSheet
from prime_jennie_runtime.telegram_bot.control import (
    KEY_FORCED_LIQUIDATION,
    STATE_KEY_LIQUIDATE_ARMED,
)

logger = logging.getLogger(__name__)

# 시트 조회 콜백 — sheet_id → 해당 시트. 캐시 구현은 호출자 책임.
SheetFetcher = Callable[[str], Awaitable[list[PositionSheet]]]
AccountSizer = Callable[[PositionSheet], Awaitable[int]]
# ticker → 일봉 종가 리스트 (oldest→newest). death_cross rule 평가용.
DailyClosesFetcher = Callable[[str], Awaitable[list[float]]]
Clock = Callable[[], datetime]


def _default_clock() -> datetime:
    return datetime.now(UTC)


class TickLoop:
    """STREAM_PRICES 소비. 한 tick 당 ticker 의 active sheet (exit) +
    pending sheet (entry condition 재평가) 를 모두 처리.
    """

    def __init__(
        self,
        redis_client: aioredis.Redis,
        tracker: PositionTracker,
        exit_executor: ExitExecutor,
        sheet_fetcher: SheetFetcher,
        pending_queue: PendingEntryQueue,
        entry_evaluator: EntryConditionEvaluator,
        entry_executor: EntryExecutor,
        account_sizer: AccountSizer,
        *,
        bar_engine: BarEngine | None = None,
        clock: Clock = _default_clock,
        group: str = "fast_loop_ticks",
        consumer: str = "fast_loop_tick_1",
        stream: str = STREAM_PRICES,
        block_ms: int = 1000,
        max_entry_rejects: int = 5,
        notifier=None,
        daily_closes_fetcher: DailyClosesFetcher | None = None,
    ):
        self._redis = redis_client
        self._tracker = tracker
        self._exit_executor = exit_executor
        self._sheet_fetcher = sheet_fetcher
        self._queue = pending_queue
        self._entry_evaluator = entry_evaluator
        self._entry_executor = entry_executor
        self._sizer = account_sizer
        # 분봉 indicator 인프라. None 이면 update 만 스킵 — 평가자는 IndicatorProvider
        # 기본 동작 (모두 None) 으로 condition 평가에서 자연스럽게 "보류".
        self._bar_engine = bar_engine
        self._clock = clock
        self._group = group
        self._consumer = consumer
        self._stream = stream
        self._block_ms = block_ms
        self._running = False
        # audit A1 fix (2026-05-15) — entry KIS reject 누적 차단.
        # 같은 sheet 가 매 tick 재시도하다 N회 도달 시 큐에서 제거 + critical alert.
        # 5-13 max_exit_failures 와 같은 원리의 entry 측 가드. KIS rate limit 폭주 방어.
        self._max_entry_rejects = max(1, max_entry_rejects)
        self._entry_reject_counts: dict[str, int] = {}
        self._notifier = notifier
        # death_cross rule 평가용 일봉 종가 fetcher + 종목별 1일 1회 캐시.
        # 미주입 시 death_cross 는 자연히 미발동 (fail-safe).
        self._daily_closes_fetcher = daily_closes_fetcher
        self._daily_closes_cache: dict[str, tuple[date, list[float]]] = {}

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
                # 일시적 redis 끊김에 fast_loop 전체를 재시작하지 않는다 —
                # 5초 후 재시도. redis 가 재기동돼 group 이 사라졌을 수 있어
                # ensure_group 도 재호출 (BUSYGROUP 은 idempotent).
                logger.exception("redis connection lost in tick_loop — retrying in 5s")
                await asyncio.sleep(5.0)
                try:
                    await self.ensure_group()
                except Exception:
                    logger.exception("ensure_group after redis reconnect failed")
                continue
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

        # 분봉 집계 — entry/exit 평가 전에 미리 누적. update 가 분봉을 닫으면
        # 이후 indicator lookup 이 새 bar 를 반영. 동기 / 락 보호.
        if self._bar_engine is not None:
            try:
                self._bar_engine.update(tick.ticker, tick.price, tick.volume, ts=tick.ts)
            except Exception:
                logger.exception("bar_engine update failed ticker=%s", tick.ticker)

        # ── forced liquidation: armed && ticker ∈ forced_liquidation 이면
        #     보유 중 sheet 들 모두 즉시 청산 (정상 exit rule 우선) ──
        await self._evaluate_forced_liquidation(tick)

        # ── exit path: 보유 중 시트 평가 ──
        await self._evaluate_exits(tick)

        # ── entry path: pending 큐 시트 conditions 재평가 ──
        await self._evaluate_pending_entries(tick)

    async def _evaluate_forced_liquidation(self, tick: TickData) -> None:
        """liquidate_armed + ticker∈forced_liquidation 이면 강제 시장가 청산.

        STOP 키가 설정돼 있으면 ExitExecutor 가 (상위 layer 에서) blocked_stop
        outcome 을 반환 → 자동 차단. 사용자가 STOP 풀고 청산하려면 /resume
        선행 필요. (사용자 mental model: STOP = global kill switch)
        """
        sheet_ids = self._tracker.sheet_ids_for(tick.ticker)
        if not sheet_ids:
            return
        armed = await self._redis.get(STATE_KEY_LIQUIDATE_ARMED)
        if not armed:
            return
        is_member = await self._redis.sismember(KEY_FORCED_LIQUIDATION, tick.ticker.encode())
        if not is_member:
            return
        for sheet_id in list(sheet_ids):
            state = self._tracker.get(sheet_id)
            if state is None:
                continue
            decision = ExitDecision(should_close=True, reason="forced_liquidation", portion=1.0)
            try:
                await self._exit_executor.execute(state, decision)
            except Exception:
                logger.exception(
                    "forced_liquidation exit raised: sheet=%s ticker=%s", sheet_id, tick.ticker
                )

    async def _evaluate_exits(self, tick: TickData) -> None:
        sheet_ids = self._tracker.sheet_ids_for(tick.ticker)
        if not sheet_ids:
            return
        # death_cross rule 평가용 일봉 종가 — 종목별 1일 1회 fetch (캐시).
        daily_closes = await self._get_daily_closes(tick.ticker, tick.ts)
        for sheet_id in sheet_ids:
            state = self._tracker.get(sheet_id)
            if state is None:
                continue
            sheets = await self._sheet_fetcher(sheet_id)
            if not sheets:
                continue
            sheet = sheets[0]
            # time_stop hold_days 평가용 — 진입 후 경과 영업일수.
            entered_business_days = _business_days_since(state.entered_at, tick.ts)
            decision = evaluate_exit(
                sheet,
                state,
                tick,
                daily_closes=daily_closes,
                entered_business_days=entered_business_days,
            )
            if decision is None:
                # state 변경 가능 (high_watermark 등) 만 persist
                try:
                    await self._tracker.persist(sheet_id)
                except Exception:
                    logger.exception("tracker.persist failed sheet=%s", sheet_id)
                continue

            # Coordinator Event Bus hook (Stage 1, best-effort).
            # exit_executor 호출 직전 — rule_type / reason 기록.
            # 실패해도 매매 path 영향 X.
            try:
                from prime_jennie_runtime.coordinator import (
                    ExitDecidedEvent,
                    publish_event,
                )

                await publish_event(
                    self._redis,
                    ExitDecidedEvent(
                        occurred_at=datetime.now(UTC),
                        correlation_id=sheet_id,
                        sheet_id=sheet_id,
                        ticker=tick.ticker,
                        rule_type=decision.reason,
                        reason=decision.reason,
                        portion=float(decision.portion),
                    ),
                )
            except Exception:
                logger.exception("coordinator publish_event(ExitDecided) failed sheet=%s", sheet_id)

            # exit_executor 호출 — KIS gateway 503 / CircuitBreakerError 가
            # tick_loop task 를 죽이지 않도록 격리 (5-13 사고 학습:
            # 한 sheet 의 실패로 fast_loop 전체가 shutdown → restart 무한 루프).
            try:
                await self._exit_executor.execute(state, decision)
            except Exception:
                logger.exception(
                    "exit_executor raised: sheet=%s ticker=%s reason=%s",
                    sheet_id,
                    tick.ticker,
                    decision.reason,
                )

    async def _get_daily_closes(self, ticker: str, now: datetime) -> list[float] | None:
        """death_cross 평가용 일봉 종가 (oldest→newest). 종목별 KST 날짜 단위 캐시.

        fetcher 미주입이거나 조회 실패 시 None — death_cross rule 은 그러면
        자연히 미발동 (`exit_evaluator._check_death_cross` 가 None 을 skip).
        """
        if self._daily_closes_fetcher is None:
            return None
        today = _kst_date(now)
        cached = self._daily_closes_cache.get(ticker)
        if cached is not None and cached[0] == today:
            return cached[1]
        try:
            closes = await self._daily_closes_fetcher(ticker)
        except Exception:
            logger.exception("daily_closes fetch failed ticker=%s", ticker)
            return cached[1] if cached is not None else None
        self._daily_closes_cache[ticker] = (today, closes)
        return closes

    async def _evaluate_pending_entries(self, tick: TickData) -> None:
        pending_sheets = self._queue.snapshot_for_ticker(tick.ticker)
        if not pending_sheets:
            return

        now = self._clock()
        for sheet in pending_sheets:
            # 평가 전 보유 dedup 재확인 (동시성 안전)
            if self._tracker.sheet_ids_for(sheet.ticker):
                logger.info(
                    "pending entry skipped (now holding): ticker=%s sheet=%s",
                    sheet.ticker,
                    sheet.sheet_id,
                )
                await self._queue.remove(sheet.sheet_id)
                continue

            result = self._entry_evaluator.evaluate(sheet, tick, now=now)
            if not result.passed:
                logger.debug(
                    "pending entry conditions not met: sheet=%s reason=%s",
                    sheet.sheet_id,
                    result.reason,
                )
                # entry.valid_until 만료면 큐에서 제거 + terminal 거부 이벤트.
                # 그 외 non-terminal (단지 not-yet-met) 은 매 tick 발생하므로
                # 이벤트 폭주 회피 위해 발행 X.
                if result.reason == "entry_valid_until_expired":
                    await self._queue.remove(sheet.sheet_id)
                    await self._emit_entry_decided(
                        sheet,
                        conditions_passed=False,
                        passed_reason=result.reason or "entry_valid_until_expired",
                        intended_quantity=0,
                    )
                continue

            # 통과 → 매수 실행
            try:
                qty = await self._sizer(sheet)
            except Exception:
                logger.exception("account_sizer failed sheet=%s", sheet.sheet_id)
                continue
            if qty <= 0:
                logger.info(
                    "pending entry skipped qty<=0 sheet=%s ticker=%s",
                    sheet.sheet_id,
                    sheet.ticker,
                )
                await self._emit_entry_decided(
                    sheet,
                    conditions_passed=True,
                    passed_reason="sizer_zero",
                    intended_quantity=0,
                )
                continue

            # Coordinator Event Bus hook (Stage 1, best-effort).
            # entry_executor 호출 직전 — passed_reason="all_passed".
            await self._emit_entry_decided(
                sheet,
                conditions_passed=True,
                passed_reason="all_passed",
                intended_quantity=qty,
            )

            try:
                outcome = await self._entry_executor.execute(sheet, quantity=qty)
            except Exception:
                logger.exception(
                    "entry_executor raised: sheet=%s ticker=%s",
                    sheet.sheet_id,
                    sheet.ticker,
                )
                continue
            if outcome.success:
                logger.info(
                    "pending entry filled: sheet=%s ticker=%s qty=%d price=%.2f",
                    sheet.sheet_id,
                    sheet.ticker,
                    outcome.filled_qty,
                    outcome.filled_price,
                )
                await self._queue.remove(sheet.sheet_id)
                self._entry_reject_counts.pop(sheet.sheet_id, None)
            else:
                # audit A1 — reject 누적 + max 도달 시 큐 제거 (KIS rate limit 방어)
                cnt = self._entry_reject_counts.get(sheet.sheet_id, 0) + 1
                self._entry_reject_counts[sheet.sheet_id] = cnt
                logger.warning(
                    "pending entry failed: sheet=%s reason=%s reject_count=%d/%d",
                    sheet.sheet_id,
                    outcome.reason,
                    cnt,
                    self._max_entry_rejects,
                )
                if cnt >= self._max_entry_rejects:
                    logger.error(
                        "pending entry max rejects — removing from queue: sheet=%s ticker=%s",
                        sheet.sheet_id,
                        sheet.ticker,
                    )
                    await self._queue.remove(sheet.sheet_id)
                    self._entry_reject_counts.pop(sheet.sheet_id, None)
                    if self._notifier is not None:
                        try:
                            from datetime import UTC, datetime

                            from prime_jennie_runtime.fast_loop.schemas import (
                                GenericAlertNotification,
                            )

                            await self._notifier.emit(
                                GenericAlertNotification(
                                    severity="critical",
                                    title="entry max rejects",
                                    body=(
                                        f"sheet={sheet.sheet_id} ticker={sheet.ticker} "
                                        f"reason={outcome.reason} count={cnt} — 큐에서 제거"
                                    ),
                                    ts=datetime.now(UTC),
                                    metadata={
                                        "component": "tick_loop",
                                        "sheet_id": sheet.sheet_id,
                                        "ticker": sheet.ticker,
                                        "reject_count": cnt,
                                        "last_reason": outcome.reason or "",
                                    },
                                )
                            )
                        except Exception:
                            logger.exception("entry max-rejects alert emit failed")

    async def _emit_entry_decided(
        self,
        sheet: PositionSheet,
        *,
        conditions_passed: bool,
        passed_reason: str,
        intended_quantity: int,
    ) -> None:
        """Coordinator Event Bus hook — entry decision 직후 발행 (best-effort).

        Stage 1 관찰 전용. 결정은 이미 끝났고 발행 실패해도 매매 path 영향 X.
        """
        try:
            from prime_jennie_runtime.coordinator import (
                EntryDecidedEvent,
                publish_event,
            )

            await publish_event(
                self._redis,
                EntryDecidedEvent(
                    occurred_at=datetime.now(UTC),
                    correlation_id=sheet.sheet_id,
                    sheet_id=sheet.sheet_id,
                    ticker=sheet.ticker,
                    conditions_passed=conditions_passed,
                    passed_reason=passed_reason,
                    intended_quantity=intended_quantity,
                ),
            )
        except Exception:
            logger.exception(
                "coordinator publish_event(EntryDecided) failed sheet=%s reason=%s",
                sheet.sheet_id,
                passed_reason,
            )


def _kst_date(dt: datetime) -> date:
    """datetime → KST 날짜. naive 는 KST 로 간주 (exit_evaluator 와 동일 관례)."""
    if dt.tzinfo is None:
        return dt.date()
    return dt.astimezone(KST).date()


def _business_days_since(entered_at: datetime, now: datetime) -> int:
    """진입일로부터 경과한 영업일 수 (KST 날짜 기준, 주말 제외).

    진입 당일 = 0, 다음 영업일 = 1. ``time_stop`` hold_days rule 평가용.
    공휴일은 영업일로 계산되는 근사 — 정확한 거래일 반영이 필요하면 거래일
    캘린더 연동을 별도 검토. 현재는 토/일만 제외한다.
    """
    entry_date = _kst_date(entered_at)
    cur_date = _kst_date(now)
    if cur_date <= entry_date:
        return 0
    count = 0
    d = entry_date + timedelta(days=1)
    while d <= cur_date:
        if d.weekday() < 5:  # 0=Mon .. 4=Fri
            count += 1
        d += timedelta(days=1)
    return count


def _parse_tick(data: dict) -> TickData | None:
    """XADD payload → TickData. `kis_gateway.streamer`가 발행하는 포맷."""
    raw_payload = data.get(b"payload") or data.get("payload")
    if raw_payload is None:
        # 일부 streamer는 개별 필드로 발행할 수 있음
        try:
            ticker = _decode(data, "ticker") or _decode(data, "stock_code") or _decode(data, "code")
            price = _decode(data, "price")
            ts = _decode(data, "ts")
            volume = _decode(data, "vol") or _decode(data, "volume")
            ask = _decode(data, "ask")
            bid = _decode(data, "bid")
            if ticker is None or price is None:
                return None
            return TickData(
                ticker=ticker,
                price=float(price),
                ts=_parse_ts(ts),
                volume=float(volume) if volume is not None else None,
                ask=_safe_float(ask),
                bid=_safe_float(bid),
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
        ticker=str(obj.get("ticker") or obj.get("stock_code") or obj.get("code")),
        price=float(obj["price"]),
        ts=_parse_ts(obj.get("ts")),
        rsi_1m=obj.get("rsi_1m"),
        volume=obj.get("volume") or obj.get("vol"),
        ask=_safe_float(obj.get("ask")),
        bid=_safe_float(obj.get("bid")),
    )


def _safe_float(v: object) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    return f


def _decode(data: dict, key: str) -> str | None:
    val = data.get(key) or data.get(key.encode())
    if val is None:
        return None
    if isinstance(val, bytes):
        return val.decode()
    return str(val)


def _parse_ts(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return datetime.now(UTC)
