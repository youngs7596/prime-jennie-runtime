"""PendingEntryQueue + EntryConditionEvaluator 회귀 테스트.

POSITION_SHEET_SPEC §4.1 / §4.3 명세 검증:
    - 시트 큐 적재 + ticker 인덱스
    - 같은 sheet_id 중복 거부 / valid_until 만료 시 purge
    - Redis 백업 + 부팅 시 load_from_redis 복구
    - conditions 빈 리스트면 즉시 통과
    - price_below / price_above 평가 정확
    - spread_under_bps 호가 평가 + no_quote 보류
    - volume_over_ma20 — provider 미주입 / warm-up / pass / fail 4 케이스
    - rsi_under — provider 미주입 / warm-up / pass / fail 4 케이스
    - 09:00 KST 이전 시트는 보류
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from prime_jennie_runtime.fast_loop.domain import TickData
from prime_jennie_runtime.fast_loop.pending_entry import (
    EntryConditionEvaluator,
    PendingEntryQueue,
)
from prime_jennie_runtime.position_sheet.schema import KST, PositionSheet


def _make_sheet(
    sheet_id: str = "ps_20260416_005930_a3f2",
    *,
    ticker: str = "005930",
    conditions: list[dict] | None = None,
    entry_valid_hours: float = 1.0,
    generated_at: datetime | None = None,
) -> PositionSheet:
    gen = generated_at or datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST)
    sheet_valid = gen + timedelta(hours=6)
    entry_valid = gen + timedelta(hours=entry_valid_hours)
    if entry_valid > sheet_valid:
        entry_valid = sheet_valid - timedelta(seconds=60)
    return PositionSheet(
        sheet_id=sheet_id,
        generated_at=gen,
        valid_until=sheet_valid,
        ticker=ticker,
        strategy_tag="SECTOR_MOMENTUM",
        size={
            "base_pct": 0.05,
            "macro_multiplier": 1.0,
            "risk_multiplier": 1.0,
            "final_pct": 0.05,
            "max_notional_krw": 5_000_000,
        },
        entry={
            "trigger": "limit",
            "price": 70000,
            "valid_until": entry_valid,
            "conditions": conditions or [],
        },
        exit={
            "rules": [
                {"type": "fixed_sl", "pct": 0.05},
                {"type": "time_stop", "mode": "eod"},
            ]
        },
        provenance={
            "scout_run_id": "scout_X",
            "scout_code_hash": "sha256:x",
            "scout_hypothesis": "test",
            "macro_state_snapshot": {
                "gate": "open",
                "size_multiplier": 1.0,
                "gate_run_id": "macro_X",
            },
            "strategy_policy_version": "v3.0.1",
            "generated_by": "test",
        },
    )


# ───────────────────────── Queue ─────────────────────────


@pytest.mark.asyncio
async def test_queue_add_and_lookup(fake_redis):
    queue = PendingEntryQueue(fake_redis)
    sheet = _make_sheet()

    added = await queue.add(sheet)
    assert added is True
    assert queue.contains_sheet(sheet.sheet_id) is True
    assert queue.contains_ticker(sheet.ticker) is True
    assert queue.snapshot_for_ticker(sheet.ticker) == [sheet]


@pytest.mark.asyncio
async def test_queue_dedup_same_sheet_id(fake_redis):
    queue = PendingEntryQueue(fake_redis)
    sheet = _make_sheet()
    assert await queue.add(sheet) is True
    assert await queue.add(sheet) is False  # 두 번째는 noop


@pytest.mark.asyncio
async def test_queue_remove(fake_redis):
    queue = PendingEntryQueue(fake_redis)
    sheet = _make_sheet()
    await queue.add(sheet)

    await queue.remove(sheet.sheet_id)

    assert queue.contains_sheet(sheet.sheet_id) is False
    assert queue.contains_ticker(sheet.ticker) is False
    # Redis 도 삭제 확인
    assert await fake_redis.get(f"pending_entry:{sheet.sheet_id}") is None


@pytest.mark.asyncio
async def test_queue_purge_expired(fake_redis):
    queue = PendingEntryQueue(fake_redis)
    # entry_valid 가 1초 후로 짧게
    s1 = _make_sheet(
        "ps_20260416_005930_a3f2",
        entry_valid_hours=0.001,  # 약 3.6초
    )
    s2 = _make_sheet("ps_20260416_005930_b1c3")  # 1시간 유효
    await queue.add(s1)
    await queue.add(s2)

    # s1.entry.valid_until 보다 한참 뒤 시각으로 purge
    future = s1.entry.valid_until + timedelta(minutes=1)
    removed = await queue.purge_expired(future)

    assert removed == 1
    assert not queue.contains_sheet(s1.sheet_id)
    assert queue.contains_sheet(s2.sheet_id)


@pytest.mark.asyncio
async def test_queue_load_from_redis(fake_redis):
    """이전 프로세스가 적재한 시트를 부팅 시 복구."""
    queue1 = PendingEntryQueue(fake_redis)
    sheet = _make_sheet()
    await queue1.add(sheet)

    # 새 큐 인스턴스가 같은 redis 에서 복구
    queue2 = PendingEntryQueue(fake_redis)
    count = await queue2.load_from_redis()

    assert count == 1
    assert queue2.contains_sheet(sheet.sheet_id) is True
    assert queue2.snapshot_for_ticker(sheet.ticker)[0].sheet_id == sheet.sheet_id


# ───────────────────────── Evaluator ─────────────────────────


def _tick(
    price: float = 70000.0,
    ticker: str = "005930",
    *,
    bid: float | None = None,
    ask: float | None = None,
) -> TickData:
    return TickData(
        ticker=ticker,
        price=price,
        ts=datetime(2026, 4, 16, 9, 30, 0, tzinfo=KST),
        bid=bid,
        ask=ask,
    )


def _market_open_now() -> datetime:
    """09:30 KST = 00:30 UTC. 명세상 장 개장 후 시각."""
    return datetime(2026, 4, 16, 9, 30, 0, tzinfo=KST).astimezone(UTC)


def test_evaluator_empty_conditions_passes():
    sheet = _make_sheet(conditions=[])
    evaluator = EntryConditionEvaluator()
    result = evaluator.evaluate(sheet, _tick(), now=_market_open_now())
    assert result.passed is True


def test_evaluator_before_market_open_blocks():
    """now 가 08:00 KST 면 보류."""
    sheet = _make_sheet(conditions=[])
    evaluator = EntryConditionEvaluator()
    pre_open = datetime(2026, 4, 16, 8, 0, 0, tzinfo=KST).astimezone(UTC)
    result = evaluator.evaluate(sheet, _tick(), now=pre_open)
    assert result.passed is False
    assert result.reason == "before_market_open"


def test_evaluator_price_below_pass():
    sheet = _make_sheet(conditions=[{"type": "price_below", "value": 71000}])
    evaluator = EntryConditionEvaluator()
    result = evaluator.evaluate(sheet, _tick(price=70000.0), now=_market_open_now())
    assert result.passed is True


def test_evaluator_price_below_fail():
    sheet = _make_sheet(conditions=[{"type": "price_below", "value": 69000}])
    evaluator = EntryConditionEvaluator()
    result = evaluator.evaluate(sheet, _tick(price=70000.0), now=_market_open_now())
    assert result.passed is False
    assert result.reason.startswith("price_below_fail")


def test_evaluator_price_above_pass():
    sheet = _make_sheet(conditions=[{"type": "price_above", "value": 69000}])
    evaluator = EntryConditionEvaluator()
    result = evaluator.evaluate(sheet, _tick(price=70000.0), now=_market_open_now())
    assert result.passed is True


def test_evaluator_price_above_fail():
    sheet = _make_sheet(conditions=[{"type": "price_above", "value": 71000}])
    evaluator = EntryConditionEvaluator()
    result = evaluator.evaluate(sheet, _tick(price=70000.0), now=_market_open_now())
    assert result.passed is False
    assert result.reason.startswith("price_above_fail")


def test_evaluator_volume_over_ma20_no_provider_blocks():
    """provider 미주입 시 warm-up 과 동일 — no_data 사유로 보류."""
    sheet = _make_sheet(conditions=[{"type": "volume_over_ma20", "min_ratio": 1.5}])
    evaluator = EntryConditionEvaluator()
    result = evaluator.evaluate(sheet, _tick(), now=_market_open_now())
    assert result.passed is False
    assert result.reason == "volume_over_ma20_no_data"


def test_evaluator_volume_over_ma20_warmup_blocks():
    """ma20 가 None (warm-up) 이면 보류."""
    from prime_jennie_runtime.fast_loop.bar_engine import IndicatorProvider

    class WarmupProvider(IndicatorProvider):
        def volume_ma20(self, ticker: str) -> float | None:
            return None

        def intraday_cum_volume(self, ticker: str) -> float | None:
            return 5_000_000.0

    sheet = _make_sheet(conditions=[{"type": "volume_over_ma20", "min_ratio": 1.5}])
    evaluator = EntryConditionEvaluator(indicators=WarmupProvider())
    result = evaluator.evaluate(sheet, _tick(), now=_market_open_now())
    assert result.passed is False
    assert result.reason == "volume_over_ma20_no_data"


def test_evaluator_volume_over_ma20_pass():
    """cum_volume / ma20 = 2.0 ≥ min_ratio 1.5 → 통과."""
    from prime_jennie_runtime.fast_loop.bar_engine import IndicatorProvider

    class Stub(IndicatorProvider):
        def volume_ma20(self, ticker: str) -> float | None:
            return 1_000_000.0

        def intraday_cum_volume(self, ticker: str) -> float | None:
            return 2_000_000.0

    sheet = _make_sheet(conditions=[{"type": "volume_over_ma20", "min_ratio": 1.5}])
    evaluator = EntryConditionEvaluator(indicators=Stub())
    result = evaluator.evaluate(sheet, _tick(), now=_market_open_now())
    assert result.passed is True
    assert result.reason == "all_passed"


def test_evaluator_volume_over_ma20_fail():
    """cum_volume / ma20 = 1.0 < min_ratio 1.5 → 차단."""
    from prime_jennie_runtime.fast_loop.bar_engine import IndicatorProvider

    class Stub(IndicatorProvider):
        def volume_ma20(self, ticker: str) -> float | None:
            return 1_000_000.0

        def intraday_cum_volume(self, ticker: str) -> float | None:
            return 1_000_000.0

    sheet = _make_sheet(conditions=[{"type": "volume_over_ma20", "min_ratio": 1.5}])
    evaluator = EntryConditionEvaluator(indicators=Stub())
    result = evaluator.evaluate(sheet, _tick(), now=_market_open_now())
    assert result.passed is False
    assert result.reason.startswith("volume_over_ma20_fail")


def test_evaluator_rsi_under_no_provider_blocks():
    """provider 미주입 시 RSI None → 보류."""
    sheet = _make_sheet(conditions=[{"type": "rsi_under", "threshold": 70}])
    evaluator = EntryConditionEvaluator()
    result = evaluator.evaluate(sheet, _tick(), now=_market_open_now())
    assert result.passed is False
    assert result.reason == "rsi_under_no_data"


def test_evaluator_rsi_under_pass():
    """RSI 55 < threshold 70 → 통과."""
    from prime_jennie_runtime.fast_loop.bar_engine import IndicatorProvider

    class Stub(IndicatorProvider):
        def rsi_1m(self, ticker: str) -> float | None:
            return 55.0

    sheet = _make_sheet(conditions=[{"type": "rsi_under", "threshold": 70}])
    evaluator = EntryConditionEvaluator(indicators=Stub())
    result = evaluator.evaluate(sheet, _tick(), now=_market_open_now())
    assert result.passed is True
    assert result.reason == "all_passed"


def test_evaluator_rsi_under_fail():
    """RSI 75 ≥ threshold 70 → 차단 (overextension)."""
    from prime_jennie_runtime.fast_loop.bar_engine import IndicatorProvider

    class Stub(IndicatorProvider):
        def rsi_1m(self, ticker: str) -> float | None:
            return 75.0

    sheet = _make_sheet(conditions=[{"type": "rsi_under", "threshold": 70}])
    evaluator = EntryConditionEvaluator(indicators=Stub())
    result = evaluator.evaluate(sheet, _tick(), now=_market_open_now())
    assert result.passed is False
    assert result.reason.startswith("rsi_under_fail")


def test_evaluator_price_above_recent_high_no_provider_blocks():
    """provider 미주입 시 recent_high None → 보류 (warm-up 등가)."""
    sheet = _make_sheet(conditions=[{"type": "price_above_recent_high", "lookback": 5}])
    evaluator = EntryConditionEvaluator()
    result = evaluator.evaluate(sheet, _tick(price=70000.0), now=_market_open_now())
    assert result.passed is False
    assert result.reason == "price_above_recent_high_no_data"


def test_evaluator_price_above_recent_high_pass():
    """price 70200 > recent_high 70100 → 통과 (breakout 확인)."""
    from prime_jennie_runtime.fast_loop.bar_engine import IndicatorProvider

    class Stub(IndicatorProvider):
        def recent_high(self, ticker: str, *, lookback: int = 5) -> float | None:
            return 70100.0

    sheet = _make_sheet(conditions=[{"type": "price_above_recent_high", "lookback": 5}])
    evaluator = EntryConditionEvaluator(indicators=Stub())
    result = evaluator.evaluate(sheet, _tick(price=70200.0), now=_market_open_now())
    assert result.passed is True


def test_evaluator_price_above_recent_high_fail():
    """price 70000 ≤ recent_high 70100 → 차단 (breakout 미확정)."""
    from prime_jennie_runtime.fast_loop.bar_engine import IndicatorProvider

    class Stub(IndicatorProvider):
        def recent_high(self, ticker: str, *, lookback: int = 5) -> float | None:
            return 70100.0

    sheet = _make_sheet(conditions=[{"type": "price_above_recent_high", "lookback": 5}])
    evaluator = EntryConditionEvaluator(indicators=Stub())
    result = evaluator.evaluate(sheet, _tick(price=70000.0), now=_market_open_now())
    assert result.passed is False
    assert result.reason.startswith("price_above_recent_high_fail")


def test_evaluator_spread_under_bps_pass():
    """bid=69990 ask=70010 → spread = 20/70000 ≈ 2.86 bps, max_bps=15 → pass."""
    sheet = _make_sheet(conditions=[{"type": "spread_under_bps", "max_bps": 15}])
    evaluator = EntryConditionEvaluator()
    tick = _tick(price=70000.0, bid=69990.0, ask=70010.0)
    result = evaluator.evaluate(sheet, tick, now=_market_open_now())
    assert result.passed is True


def test_evaluator_spread_under_bps_fail():
    """bid=69500 ask=70500 → spread = 1000/70000 ≈ 142.9 bps, max_bps=15 → fail."""
    sheet = _make_sheet(conditions=[{"type": "spread_under_bps", "max_bps": 15}])
    evaluator = EntryConditionEvaluator()
    tick = _tick(price=70000.0, bid=69500.0, ask=70500.0)
    result = evaluator.evaluate(sheet, tick, now=_market_open_now())
    assert result.passed is False
    assert result.reason.startswith("spread_under_bps_fail")


def test_evaluator_spread_under_bps_no_quote():
    """bid/ask 없으면 평가 불가 → 보류."""
    sheet = _make_sheet(conditions=[{"type": "spread_under_bps", "max_bps": 15}])
    evaluator = EntryConditionEvaluator()
    result = evaluator.evaluate(sheet, _tick(), now=_market_open_now())
    assert result.passed is False
    assert result.reason == "spread_under_bps_no_quote"


def test_evaluator_and_semantics():
    """conditions 두 개 — 하나라도 fail 이면 전체 fail."""
    sheet = _make_sheet(
        conditions=[
            {"type": "price_above", "value": 69000},  # pass at price=70000
            {"type": "price_below", "value": 69500},  # fail at price=70000
        ]
    )
    evaluator = EntryConditionEvaluator()
    result = evaluator.evaluate(sheet, _tick(price=70000.0), now=_market_open_now())
    assert result.passed is False
    assert result.reason.startswith("price_below_fail")


def test_evaluator_entry_valid_until_expired():
    """entry.valid_until 지난 시트는 expired 사유로 거부."""
    gen = datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST)
    sheet = _make_sheet(generated_at=gen, entry_valid_hours=0.5)  # entry 만료 09:45 KST
    evaluator = EntryConditionEvaluator()
    after_expiry = datetime(2026, 4, 16, 11, 0, 0, tzinfo=KST).astimezone(UTC)
    result = evaluator.evaluate(sheet, _tick(), now=after_expiry)
    assert result.passed is False
    assert result.reason == "entry_valid_until_expired"
