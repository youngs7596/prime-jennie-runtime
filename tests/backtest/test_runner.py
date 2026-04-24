"""`simulate_sheet` 가 각 exit rule 을 올바르게 트리거하는지 일봉 합성으로 검증.

fast_loop.exit_evaluator 를 재사용하므로 이 테스트는 사실상 "white-box 통합
테스트" — runner 가 올바른 순서/데이터로 evaluator 를 호출하는지 확인한다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from prime_jennie_runtime.backtest import (
    DailyBar,
    simulate_sheet,
)
from prime_jennie_runtime.position_sheet.schema import (
    KST,
    EntrySection,
    ExitSection,
    FixedSlRule,
    FixedTpRule,
    MacroStateSnapshot,
    PositionSheet,
    PriceAboveCondition,
    ProfitFloorRule,
    ProvenanceSection,
    ScaleOutRule,
    SizeSection,
    TimeStopRule,
    TrailingTpRule,
)

# ---------------------------------------------------------------------------
# 헬퍼 — 유효한 PositionSheet 생성
# ---------------------------------------------------------------------------


def _mk_sheet(
    *,
    rules,
    entry_trigger: str = "market",
    entry_price: float | None = None,
    entry_conditions: list | None = None,
    generated_date: date = date(2026, 5, 4),
    ticker: str = "005930",
) -> PositionSheet:
    generated_at = datetime.combine(generated_date, datetime.min.time(), tzinfo=KST).replace(
        hour=9, minute=0
    )
    valid_until = generated_at.replace(hour=15, minute=20)
    return PositionSheet(
        sheet_id=f"ps_{generated_date.strftime('%Y%m%d')}_090000_a1b2",
        schema_version="1.1",
        generated_at=generated_at,
        valid_until=valid_until,
        ticker=ticker,
        side="long",
        strategy_tag="GAP_UP_REBOUND",
        size=SizeSection(
            base_pct=0.05,
            macro_multiplier=1.0,
            risk_multiplier=1.0,
            final_pct=0.05,
            max_notional_krw=5_000_000,
        ),
        entry=EntrySection(
            trigger=entry_trigger,
            price=entry_price,
            valid_until=valid_until,
            conditions=entry_conditions or [],
        ),
        exit=ExitSection(rules=rules),
        provenance=ProvenanceSection(
            scout_run_id="scout_test",
            scout_code_hash="sha256:test",
            scout_hypothesis="test",
            macro_state_snapshot=MacroStateSnapshot(
                gate="open", size_multiplier=1.0, gate_run_id="macro_test"
            ),
            strategy_policy_version="test",
            generated_by="test",
        ),
    )


def _bars(start: date, ohlcv: list[tuple[float, float, float, float]]) -> list[DailyBar]:
    """(open, high, low, close) 튜플 리스트 → DailyBar 리스트. 주말 스킵."""
    bars: list[DailyBar] = []
    d = start
    for o, h, lo, c in ohlcv:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        bars.append(
            DailyBar(
                ticker="005930",
                price_date=d,
                open=o,
                high=h,
                low=lo,
                close=c,
                volume=1_000_000,
            )
        )
        d += timedelta(days=1)
    return bars


# ---------------------------------------------------------------------------
# Entry 체결
# ---------------------------------------------------------------------------


def test_market_entry_fills_at_open_with_slippage():
    sheet = _mk_sheet(
        rules=[
            FixedSlRule(pct=0.05),
            TimeStopRule(mode="hold_days", value=10),
        ]
    )
    bars = _bars(date(2026, 5, 4), [(10_000, 10_000, 10_000, 10_000)] * 3)
    result = simulate_sheet(sheet, bars, capital_krw=1_000_000)
    assert result.filled
    # slippage 0.1% 반영
    assert result.entry_price == pytest.approx(10_010.0, abs=0.01)
    assert result.trades[0].side == "buy"
    assert result.trades[0].qty == int(1_000_000 // 10_010)


def test_limit_entry_unfilled_when_low_above_limit():
    sheet = _mk_sheet(
        rules=[FixedSlRule(pct=0.05), TimeStopRule(mode="eod")],
        entry_trigger="limit",
        entry_price=9_000.0,
    )
    bars = _bars(date(2026, 5, 4), [(10_000, 10_200, 9_800, 10_100)])
    result = simulate_sheet(sheet, bars, capital_krw=1_000_000)
    assert not result.filled
    assert result.exit_reason == "limit_unfilled"


def test_limit_entry_fills_when_low_touches():
    sheet = _mk_sheet(
        rules=[FixedSlRule(pct=0.05), TimeStopRule(mode="hold_days", value=5)],
        entry_trigger="limit",
        entry_price=9_900.0,
    )
    bars = _bars(
        date(2026, 5, 4),
        [(10_000, 10_100, 9_800, 10_050)] + [(10_050, 10_050, 10_050, 10_050)] * 2,
    )
    result = simulate_sheet(sheet, bars, capital_krw=1_000_000)
    assert result.filled
    # open(10000) > limit(9900) → limit 에 체결 + slippage
    assert result.entry_price == pytest.approx(9_909.9, abs=0.01)


def test_entry_condition_price_above_not_met_fails_fill():
    sheet = _mk_sheet(
        rules=[FixedSlRule(pct=0.05), TimeStopRule(mode="eod")],
        entry_conditions=[PriceAboveCondition(value=11_000.0)],
    )
    bars = _bars(date(2026, 5, 4), [(10_000, 10_500, 9_800, 10_200)])
    result = simulate_sheet(sheet, bars, capital_krw=1_000_000)
    assert not result.filled
    assert result.exit_reason == "condition_failed"


# ---------------------------------------------------------------------------
# Exit rules
# ---------------------------------------------------------------------------


def test_fixed_sl_triggers_on_low():
    sheet = _mk_sheet(
        rules=[
            FixedSlRule(pct=0.05),
            TimeStopRule(mode="hold_days", value=30),
        ]
    )
    # entry=10010, sl=-5% → threshold≈9510. day2 low=9400 트리거
    bars = _bars(
        date(2026, 5, 4),
        [
            (10_000, 10_100, 9_900, 10_000),  # day0 entry
            (9_950, 10_000, 9_400, 9_500),  # day1 sl 트리거
        ],
    )
    result = simulate_sheet(sheet, bars, capital_krw=1_000_000)
    assert result.filled
    assert result.exit_reason == "fixed_sl"
    assert result.pnl_pct < 0


def test_fixed_tp_triggers_on_high():
    sheet = _mk_sheet(
        rules=[
            FixedSlRule(pct=0.10),
            FixedTpRule(pct=0.05),
            TimeStopRule(mode="hold_days", value=30),
        ]
    )
    bars = _bars(
        date(2026, 5, 4),
        [
            (10_000, 10_100, 9_900, 10_000),  # entry at 10010
            (10_000, 10_700, 9_900, 10_500),  # high=10700 → +6.9%, TP 5% 트리거
        ],
    )
    result = simulate_sheet(sheet, bars, capital_krw=1_000_000)
    assert result.filled
    assert result.exit_reason == "fixed_tp"
    assert result.pnl_pct > 0


def test_trailing_tp_activates_then_drops():
    sheet = _mk_sheet(
        rules=[
            TrailingTpRule(activate_pct=0.05, drop_pct=0.03),
            FixedSlRule(pct=0.10),
            TimeStopRule(mode="hold_days", value=30),
        ]
    )
    # entry 10010 → day1 high 11000 (+9.9%) activate → low 10500 (~-4.5% from peak) trigger
    bars = _bars(
        date(2026, 5, 4),
        [
            (10_000, 10_100, 9_900, 10_000),  # entry
            (10_500, 11_000, 10_500, 10_800),  # activate at high
            (10_600, 10_700, 10_500, 10_600),  # drop > 3% from 11000
        ],
    )
    result = simulate_sheet(sheet, bars, capital_krw=1_000_000)
    assert result.filled
    assert result.exit_reason == "trailing_tp"


def test_profit_floor_triggers_after_peak_then_decline():
    sheet = _mk_sheet(
        rules=[
            ProfitFloorRule(activate_pct=0.10, floor_pct=0.05),
            FixedSlRule(pct=0.10),
            TimeStopRule(mode="hold_days", value=30),
        ]
    )
    # entry 10010, peak activate >= +10% (high 11100) → current return < +5% 시 청산
    bars = _bars(
        date(2026, 5, 4),
        [
            (10_000, 10_100, 9_900, 10_000),  # entry
            (10_500, 11_200, 10_500, 11_000),  # peak activate
            (10_900, 10_900, 10_300, 10_400),  # current return ~+3.9%, floor break
        ],
    )
    result = simulate_sheet(sheet, bars, capital_krw=1_000_000)
    assert result.filled
    assert result.exit_reason == "profit_floor"


def test_scale_out_partial_then_time_stop():
    sheet = _mk_sheet(
        rules=[
            ScaleOutRule(levels=[(0.03, 0.5)]),  # +3% 에서 50% 부분 청산
            FixedSlRule(pct=0.10),
            TimeStopRule(mode="hold_days", value=2),
        ]
    )
    bars = _bars(
        date(2026, 5, 4),
        [
            (10_000, 10_100, 9_900, 10_000),  # entry
            (10_100, 10_400, 10_000, 10_300),  # +3.9% scale_out 50%
            (10_300, 10_400, 10_200, 10_300),  # hold_days=2, eod 청산
        ],
    )
    result = simulate_sheet(sheet, bars, capital_krw=1_000_000)
    assert result.filled
    # entry + partial sell + final sell
    sell_trades = [t for t in result.trades if t.side == "sell"]
    assert len(sell_trades) == 2
    assert sell_trades[0].reason == "scale_out"
    assert sell_trades[1].reason == "time_stop"
    # 부분 청산 수량 합 == 전체 수량
    assert sum(t.qty for t in sell_trades) == result.total_qty


def test_time_stop_eod_triggers_same_day():
    sheet = _mk_sheet(
        rules=[
            FixedSlRule(pct=0.10),
            TimeStopRule(mode="eod"),
        ]
    )
    # 진입 다음 일봉의 close tick (15:30 KST) 에서 eod 트리거
    bars = _bars(
        date(2026, 5, 4),
        [
            (10_000, 10_100, 9_900, 10_000),  # entry
            (10_000, 10_050, 9_950, 10_020),  # eod 청산
        ],
    )
    result = simulate_sheet(sheet, bars, capital_krw=1_000_000)
    assert result.filled
    assert result.exit_reason == "time_stop"
    # entry + 1 exit
    assert len([t for t in result.trades if t.side == "sell"]) == 1


def test_time_stop_hold_days_triggers_after_n_days():
    sheet = _mk_sheet(
        rules=[
            FixedSlRule(pct=0.10),
            TimeStopRule(mode="hold_days", value=3),
        ]
    )
    # 4 bar: entry + 3 days → 3rd day after entry 15:20 이후 eod 트리거
    bars = _bars(
        date(2026, 5, 4),
        [
            (10_000, 10_100, 9_900, 10_000),  # entry (day 0)
            (10_000, 10_050, 9_950, 10_000),  # day 1 — 아직 안 나감
            (10_000, 10_050, 9_950, 10_000),  # day 2 — 아직 안 나감
            (10_000, 10_050, 9_950, 10_000),  # day 3 — 트리거
        ],
    )
    result = simulate_sheet(sheet, bars, capital_krw=1_000_000)
    assert result.filled
    assert result.exit_reason == "time_stop"
    # 정확히 3 거래일 후 청산
    assert result.exit_ts is not None
    assert (result.exit_ts.date() - bars[0].price_date).days >= 3


def test_data_end_forces_close():
    """exit 룰이 발동되지 않고 bar 가 끝나면 마지막 close 로 강제 청산."""
    sheet = _mk_sheet(
        rules=[
            FixedSlRule(pct=0.10),
            TimeStopRule(mode="hold_days", value=100),  # 멀리 설정
        ]
    )
    bars = _bars(
        date(2026, 5, 4),
        [
            (10_000, 10_100, 9_900, 10_000),  # entry
            (10_000, 10_050, 9_950, 10_020),
            (10_020, 10_050, 9_950, 10_010),
        ],
    )
    result = simulate_sheet(sheet, bars, capital_krw=1_000_000)
    assert result.filled
    assert result.exit_reason == "data_end"


# ---------------------------------------------------------------------------
# 불변식
# ---------------------------------------------------------------------------


def test_quantity_conservation():
    """buy qty == sum(sell qty)."""
    sheet = _mk_sheet(
        rules=[
            ScaleOutRule(levels=[(0.02, 0.3), (0.05, 0.3)]),
            FixedSlRule(pct=0.10),
            TimeStopRule(mode="hold_days", value=10),
        ]
    )
    bars = _bars(
        date(2026, 5, 4),
        [
            (10_000, 10_100, 9_900, 10_000),  # entry
            (10_100, 10_300, 10_050, 10_250),  # level 0 trigger
            (10_250, 10_600, 10_200, 10_550),  # level 1 trigger
            (10_550, 10_600, 10_500, 10_550),  # remaining — 진행
        ]
        + [(10_550, 10_600, 10_500, 10_550)] * 10,
    )
    result = simulate_sheet(sheet, bars, capital_krw=1_000_000)
    assert result.filled
    buy_qty = sum(t.qty for t in result.trades if t.side == "buy")
    sell_qty = sum(t.qty for t in result.trades if t.side == "sell")
    assert buy_qty == sell_qty


def test_capital_too_small_returns_unfilled():
    sheet = _mk_sheet(
        rules=[FixedSlRule(pct=0.05), TimeStopRule(mode="eod")],
    )
    bars = _bars(date(2026, 5, 4), [(10_000, 10_100, 9_900, 10_000)] * 2)
    # capital 10 → int(10 // 10010) = 0
    result = simulate_sheet(sheet, bars, capital_krw=10.0)
    assert not result.filled
    assert result.exit_reason == "capital_too_small"


def test_empty_bars_returns_unfilled():
    sheet = _mk_sheet(rules=[FixedSlRule(pct=0.05), TimeStopRule(mode="eod")])
    result = simulate_sheet(sheet, [], capital_krw=1_000_000)
    assert not result.filled
    assert result.exit_reason == "no_bars"


# 미사용 import 제거 방지 — date 는 위 헬퍼에서 참조
_ = (date, datetime, timezone, UTC)
