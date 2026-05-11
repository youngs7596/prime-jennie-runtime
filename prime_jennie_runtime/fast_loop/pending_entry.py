"""Pending Entry Queue + Entry Condition Evaluator.

POSITION_SHEET_SPEC.md §4.1 / §4.3 의 entry conditions 평가 + 재평가 path 를
실제로 구현한다. 시트가 도착하면 즉시 매수하지 않고 큐에 적재한 뒤,
TickLoop 가 매 tick 마다 conditions 를 재평가하여 만족하는 첫 tick 에
entry_executor 를 호출한다. valid_until 만료 시 시트를 큐에서 제거.

Redis 백업:
    - ``pending_entry:{sheet_id}`` → 시트 JSON. valid_until 까지 EXPIRE
    - ``pending_entry:by_ticker:{ticker}`` → set of sheet_ids
    - 부팅 시 ``load_from_redis()`` 로 in-memory 인덱스 복구

EntryConditionEvaluator 는 6종 conditions 를 평가:
    - ``price_below`` / ``price_above`` — tick.price 단독 비교
    - ``spread_under_bps`` — bid/ask 호가 (KIS H0STCNT0 호가1)
    - ``volume_over_ma20`` — ``BarEngine`` 의 1분봉 누적 / 분봉 ma20
    - ``rsi_under`` — ``BarEngine`` 의 1분봉 RSI (overextension filter)
    - ``price_above_recent_high`` — 최근 lookback 분봉 high 돌파 (breakout)

후자 셋은 indicator warm-up 기간 (분봉 history 부족) 동안 None → "보류"
(False) 가 자연스러운 fail-open 동작. 시트의 conditions 가 비어있으면 즉시
True 를 반환하므로 정책 부착 전까진 인프라만 안전히 도는 모드.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, timezone
from typing import Any

import redis.asyncio as aioredis

from prime_jennie_runtime.fast_loop.bar_engine import IndicatorProvider
from prime_jennie_runtime.fast_loop.domain import TickData
from prime_jennie_runtime.position_sheet.schema import PositionSheet

logger = logging.getLogger(__name__)

ENTRY_KEY_PREFIX = "pending_entry:"
TICKER_INDEX_PREFIX = "pending_entry:by_ticker:"

KST = timezone(timedelta(hours=9))
MARKET_OPEN_TIME = time(9, 0)  # 09:00 KST 동시호가 직전 재평가 명세


def _entry_key(sheet_id: str) -> str:
    return f"{ENTRY_KEY_PREFIX}{sheet_id}"


def _ticker_index_key(ticker: str) -> str:
    return f"{TICKER_INDEX_PREFIX}{ticker}"


# ───────────────────────── Queue ─────────────────────────


class PendingEntryQueue:
    """sheet_id → PositionSheet + ticker → sheet_ids 역인덱스.

    in-memory dict + Redis 백업. 단일 fast-loop 프로세스 가정.
    """

    def __init__(self, redis_client: aioredis.Redis) -> None:
        self._redis = redis_client
        self._sheets: dict[str, PositionSheet] = {}
        self._by_ticker: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    # ── 조회 ──

    def contains_sheet(self, sheet_id: str) -> bool:
        return sheet_id in self._sheets

    def contains_ticker(self, ticker: str) -> bool:
        return bool(self._by_ticker.get(ticker))

    def snapshot_for_ticker(self, ticker: str) -> list[PositionSheet]:
        ids = self._by_ticker.get(ticker)
        if not ids:
            return []
        return [self._sheets[sid] for sid in sorted(ids) if sid in self._sheets]

    def all_sheet_ids(self) -> list[str]:
        return sorted(self._sheets.keys())

    def size(self) -> int:
        return len(self._sheets)

    # ── 변경 ──

    async def add(self, sheet: PositionSheet) -> bool:
        """새 시트 적재. 이미 있으면 False, 신규면 True.

        Redis 에 시트 JSON 을 저장하고 valid_until 까지 EXPIRE 설정.
        """
        async with self._lock:
            if sheet.sheet_id in self._sheets:
                return False
            self._sheets[sheet.sheet_id] = sheet
            self._by_ticker.setdefault(sheet.ticker, set()).add(sheet.sheet_id)

        try:
            await self._persist(sheet)
        except Exception:
            logger.exception("pending_entry persist failed sheet=%s", sheet.sheet_id)
        return True

    async def remove(self, sheet_id: str) -> None:
        """시트 제거 (체결 후 또는 만료 후 호출)."""
        async with self._lock:
            sheet = self._sheets.pop(sheet_id, None)
            if sheet is None:
                return
            ids = self._by_ticker.get(sheet.ticker)
            if ids is not None:
                ids.discard(sheet_id)
                if not ids:
                    del self._by_ticker[sheet.ticker]

        try:
            await self._redis.delete(_entry_key(sheet_id))
            await self._redis.srem(_ticker_index_key(sheet.ticker), sheet_id)
        except Exception:
            logger.exception("pending_entry remove redis failed sheet=%s", sheet_id)

    async def purge_expired(self, now: datetime) -> int:
        """entry.valid_until 이 지난 시트를 큐에서 제거. 제거된 개수 반환."""
        expired: list[str] = []
        for sheet_id, sheet in list(self._sheets.items()):
            if sheet.entry.valid_until <= now:
                expired.append(sheet_id)
        for sheet_id in expired:
            await self.remove(sheet_id)
            logger.info("pending_entry expired sheet=%s", sheet_id)
        return len(expired)

    # ── 부팅 복구 ──

    async def load_from_redis(self) -> int:
        """Redis 에 남아있는 pending_entry:{*} 를 in-memory 로 복원."""
        cursor = 0
        count = 0
        while True:
            cursor, keys = await self._redis.scan(cursor, match=f"{ENTRY_KEY_PREFIX}*", count=100)
            for raw_key in keys:
                key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
                # by_ticker 인덱스 키는 별도 prefix 면 필터
                if key.startswith(TICKER_INDEX_PREFIX):
                    continue
                raw = await self._redis.get(key)
                if raw is None:
                    continue
                try:
                    payload = raw.decode() if isinstance(raw, bytes) else raw
                    sheet = PositionSheet.model_validate_json(payload)
                except Exception:
                    logger.exception("invalid pending_entry payload key=%s", key)
                    continue
                self._sheets[sheet.sheet_id] = sheet
                self._by_ticker.setdefault(sheet.ticker, set()).add(sheet.sheet_id)
                count += 1
            if cursor == 0:
                break
        logger.info("pending_entry queue restored %d sheets from redis", count)
        return count

    async def purge_loop(self, *, interval_sec: float = 60.0) -> None:
        """백그라운드 purge task. fast_loop runner 가 create_task 로 띄움."""
        while True:
            try:
                now = datetime.now(UTC)
                removed = await self.purge_expired(now)
                if removed:
                    logger.info("pending_entry purge_loop removed=%d", removed)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("pending_entry purge_loop iteration failed")
            await asyncio.sleep(interval_sec)

    # ── 내부 ──

    async def _persist(self, sheet: PositionSheet) -> None:
        ttl_sec = max(int((sheet.entry.valid_until - datetime.now(UTC)).total_seconds()), 1)
        payload = sheet.model_dump_json()
        await self._redis.set(_entry_key(sheet.sheet_id), payload, ex=ttl_sec)
        await self._redis.sadd(_ticker_index_key(sheet.ticker), sheet.sheet_id)


# ───────────────────────── Evaluator ─────────────────────────


@dataclass(frozen=True)
class EvaluationResult:
    passed: bool
    reason: str = ""  # 디버깅 / 로그용


class EntryConditionEvaluator:
    """시트 + tick → conditions AND 평가.

    POSITION_SHEET_SPEC §4.2 + design Phase 0 §4.5 확장:
        price_below / price_above / spread_under_bps / volume_over_ma20 /
        rsi_under / price_above_recent_high

    indicator provider 미주입 시 ``volume_over_ma20`` / ``rsi_under`` /
    ``price_above_recent_high`` 는 warm-up 상태와 동일하게 "보류" 처리 (False).
    conditions 가 비어있으면 즉시 통과 (현 정책 = 인프라만 도입한 minimum mode).
    """

    def __init__(self, indicators: IndicatorProvider | None = None) -> None:
        # None 주입 시 모든 indicator lookup 이 None → warm-up 과 동일하게
        # 거동. 백테스트 / 기존 테스트는 indicators=None 으로 호출 호환.
        self._indicators: IndicatorProvider = indicators or IndicatorProvider()

    def evaluate(
        self,
        sheet: PositionSheet,
        tick: TickData,
        *,
        now: datetime,
    ) -> EvaluationResult:
        # 1) 장 개장 대기 — generated_at 이 09:00 KST 이전이면 보류
        if not _is_after_market_open(now):
            return EvaluationResult(False, "before_market_open")

        # 2) entry.valid_until 지난 시트는 평가 불가 (purge 가 정리하지만 race 방지)
        if sheet.entry.valid_until <= now:
            return EvaluationResult(False, "entry_valid_until_expired")

        conditions = sheet.entry.conditions
        if not conditions:
            return EvaluationResult(True, "no_conditions")

        for cond in conditions:
            ok, reason = _evaluate_one(cond, tick, self._indicators)
            if not ok:
                return EvaluationResult(False, reason)
        return EvaluationResult(True, "all_passed")


def _evaluate_one(cond: Any, tick: TickData, indicators: IndicatorProvider) -> tuple[bool, str]:
    """단일 condition 평가. (passed, reason) 반환."""
    cond_type = getattr(cond, "type", None)
    if cond_type == "price_below":
        if tick.price < float(cond.value):
            return True, "price_below_ok"
        return False, f"price_below_fail price={tick.price} threshold={cond.value}"
    if cond_type == "price_above":
        if tick.price > float(cond.value):
            return True, "price_above_ok"
        return False, f"price_above_fail price={tick.price} threshold={cond.value}"
    if cond_type == "volume_over_ma20":
        # 분봉 누적 / 분봉 거래량 ma20 ≥ min_ratio.
        # warm-up (history < 20) 이거나 provider 미주입이면 None → 보류.
        ma20 = indicators.volume_ma20(tick.ticker)
        cum_vol = indicators.intraday_cum_volume(tick.ticker)
        if ma20 is None or cum_vol is None:
            return False, "volume_over_ma20_no_data"
        if ma20 <= 0:
            # 거래량이 0 인 분봉만 누적된 경우 — 데이터 의심, 보류
            return False, "volume_over_ma20_zero_ma"
        ratio = cum_vol / ma20
        min_ratio = float(cond.min_ratio)
        if ratio >= min_ratio:
            return True, f"volume_over_ma20_ok ratio={ratio:.2f}"
        return False, f"volume_over_ma20_fail ratio={ratio:.2f} min={min_ratio}"
    if cond_type == "rsi_under":
        # 1분봉 RSI ≥ threshold 면 차단 (overextension filter). RSI 가 None
        # (warm-up) 이면 보류 — 안전 측면. period 충족 전 신규 진입은 보수적.
        rsi = indicators.rsi_1m(tick.ticker)
        if rsi is None:
            return False, "rsi_under_no_data"
        threshold = float(cond.threshold)
        if rsi < threshold:
            return True, f"rsi_under_ok rsi={rsi:.1f}"
        return False, f"rsi_under_fail rsi={rsi:.1f} threshold={threshold}"
    if cond_type == "price_above_recent_high":
        # 최근 lookback 개 분봉 high 돌파 시 진입 (GAP_UP_REBOUND breakout
        # confirmation). recent_high None → warm-up → 보류.
        lookback = int(getattr(cond, "lookback", 5))
        recent_high = indicators.recent_high(tick.ticker, lookback=lookback)
        if recent_high is None:
            return False, "price_above_recent_high_no_data"
        if tick.price > recent_high:
            return True, f"price_above_recent_high_ok price={tick.price} high={recent_high}"
        return False, (
            f"price_above_recent_high_fail price={tick.price} high={recent_high} "
            f"lookback={lookback}"
        )
    if cond_type == "spread_under_bps":
        # KIS H0STCNT0 호가1 — bid / ask 모두 양수일 때만 평가 가능
        if tick.ask is None or tick.bid is None or tick.ask <= 0 or tick.bid <= 0:
            return False, "spread_under_bps_no_quote"
        mid = (tick.ask + tick.bid) / 2.0
        if mid <= 0:
            return False, "spread_under_bps_invalid_mid"
        bps = (tick.ask - tick.bid) / mid * 10000.0
        max_bps = float(cond.max_bps)
        if bps <= max_bps:
            return True, f"spread_under_bps_ok bps={bps:.1f}"
        return False, f"spread_under_bps_fail bps={bps:.1f} max={max_bps}"
    logger.warning("unknown entry condition type=%s", cond_type)
    return False, f"unknown_condition_type:{cond_type}"


def _is_after_market_open(now: datetime) -> bool:
    """now 가 한국 시장 개장 (09:00 KST) 이후인지 판정.

    naive datetime 은 UTC 로 간주.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    kst = now.astimezone(KST)
    return kst.time() >= MARKET_OPEN_TIME


__all__ = [
    "PendingEntryQueue",
    "EntryConditionEvaluator",
    "EvaluationResult",
]
