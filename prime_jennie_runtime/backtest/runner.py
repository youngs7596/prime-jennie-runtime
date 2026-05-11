"""PositionSheet 단일 시뮬레이터.

fast_loop.exit_evaluator 를 **그대로 호출**해서 운영 경로와 동일한 exit rule
로직으로 백테스트한다. 일봉 기반이라 하루를 open/high/low/close 4 tick 으로
분해해서 먹인다. 분봉 전용 rule (overextension_exit) 은 rsi_1m=None 이면
자연스럽게 건너뛴다.

death_cross 는 일봉 MA 기반이라 close tick 에서만 daily_closes 를 전달한다.
open/high/low 에선 daily_closes=None — evaluator 가 즉시 None 반환하도록.

scale_out 은 내부에서 연속 실행 가능하도록 tick 루프가 decision.portion<1.0
일 때 trade 만 기록하고 다음 tick 으로 이어간다. PositionState 의
scale_out_executed 가 중복 방지.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

from prime_jennie_runtime.fast_loop.domain import PositionState, TickData
from prime_jennie_runtime.fast_loop.exit_evaluator import evaluate
from prime_jennie_runtime.position_sheet.schema import (
    KST,
    PositionSheet,
    PriceAboveCondition,
    PriceBelowCondition,
    VolumeOverMa20Condition,
)

from .domain import BacktestConfig, DailyBar, SheetBacktestResult, Trade

# 일봉 1 개를 4 tick 으로 분해할 때 쓰는 KST 시각
_OPEN_TIME = time(9, 0, tzinfo=KST)
_HIGH_TIME = time(10, 0, tzinfo=KST)
_LOW_TIME = time(13, 0, tzinfo=KST)
_CLOSE_TIME = time(15, 30, tzinfo=KST)  # TIME_STOP_CUTOFF(15:20) 지나서 eod 트리거


@dataclass(frozen=True)
class _DayTick:
    price: float
    ts: datetime
    is_close: bool


def _day_ticks(bar: DailyBar) -> list[_DayTick]:
    """하루를 open → high → low → close 순서의 4 tick 으로 분해.

    low 를 high 보다 뒤에 두면 trailing_tp activate 후 drop 트리거 경로를
    보수적으로 시뮬한다 (pessimistic exit). 실제 분봉 순서는 알 수 없음.
    """
    d = bar.price_date
    return [
        _DayTick(bar.open, datetime.combine(d, _OPEN_TIME), False),
        _DayTick(bar.high, datetime.combine(d, _HIGH_TIME), False),
        _DayTick(bar.low, datetime.combine(d, _LOW_TIME), False),
        _DayTick(bar.close, datetime.combine(d, _CLOSE_TIME), True),
    ]


def _entry_conditions_ok(sheet: PositionSheet, bar: DailyBar, prior_bars: list[DailyBar]) -> bool:
    """entry.conditions 를 OHLCV 범위로 근사 평가. spread_under_bps 는 스킵 (체결 가정)."""
    for cond in sheet.entry.conditions:
        if isinstance(cond, PriceBelowCondition):
            if bar.low > cond.value:
                return False
        elif isinstance(cond, PriceAboveCondition):
            if bar.high < cond.value:
                return False
        elif isinstance(cond, VolumeOverMa20Condition):
            if len(prior_bars) < 20:
                continue
            ma20 = sum(b.volume for b in prior_bars[-20:]) / 20.0
            if ma20 <= 0:
                continue
            if bar.volume < ma20 * cond.min_ratio:
                return False
        # SpreadUnderBpsCondition: OHLCV 만으로 평가 불가 → 체결 가정 (스킵)
    return True


def _effective_slippage_pct(
    config: BacktestConfig,
    *,
    raw_price: float,
    qty: int,
    bar_volume: int,
) -> float:
    """체결 1건의 effective slippage % (base + volume 가산).

    volume_based_slippage_enabled 가 False 거나 bar_volume 이 0 이면 base 만 반환.
    그 외에는 participation = (qty / bar_volume) 을 max_participation_clip 로 클립,
    extra = base × multiplier × participation 을 더한다.
    """
    base = config.slippage_pct
    if not config.volume_based_slippage_enabled or bar_volume <= 0 or qty <= 0:
        return base
    _ = raw_price  # price 변수는 향후 sqrt-impact 모델 전환 시 사용 — 현재 미사용.
    participation = qty / float(bar_volume)
    if participation > config.max_participation_clip:
        participation = config.max_participation_clip
    extra = base * config.participation_slippage_multiplier * participation
    return base + extra


def _try_entry(
    sheet: PositionSheet,
    entry_bar: DailyBar,
    prior_bars: list[DailyBar],
    config: BacktestConfig,
    *,
    qty_hint: int = 0,
) -> tuple[float | None, datetime | None, str]:
    """진입 체결 시뮬. (fill_price, fill_ts, reason) 반환. fill_price=None 이면 미체결.

    qty_hint 가 0 이면 volume-based slippage 는 skip (base 만). caller 가
    `_estimate_entry_qty` 로 1차 추정 후 다시 호출하는 2-step 흐름을 사용한다.
    """
    if not _entry_conditions_ok(sheet, entry_bar, prior_bars):
        return None, None, "condition_failed"

    entry_dt = datetime.combine(entry_bar.price_date, _OPEN_TIME)
    if sheet.entry.trigger == "market":
        slip_pct = _effective_slippage_pct(
            config, raw_price=entry_bar.open, qty=qty_hint, bar_volume=entry_bar.volume
        )
        fill = entry_bar.open * (1.0 + slip_pct / 100.0)
        return fill, entry_dt, "market"

    # limit: long-side 체결 조건 = bar.low <= limit_price
    limit = sheet.entry.price
    if limit is None:
        return None, None, "no_limit_price"
    if entry_bar.low > limit:
        return None, None, "limit_unfilled"
    # open 이 이미 limit 이하면 open 에서 체결, 아니면 limit 에서 체결
    raw_fill = min(limit, entry_bar.open)
    slip_pct = _effective_slippage_pct(
        config, raw_price=raw_fill, qty=qty_hint, bar_volume=entry_bar.volume
    )
    fill = raw_fill * (1.0 + slip_pct / 100.0)
    return fill, entry_dt, "limit"


def _exit_fill_price(
    tick_price: float,
    config: BacktestConfig,
    *,
    qty: int = 0,
    bar_volume: int = 0,
) -> float:
    """매도 시 슬리피지는 불리하게 (가격을 낮춰)."""
    slip_pct = _effective_slippage_pct(
        config, raw_price=tick_price, qty=qty, bar_volume=bar_volume
    )
    return tick_price * (1.0 - slip_pct / 100.0)


def simulate_sheet(
    sheet: PositionSheet,
    bars: list[DailyBar],
    *,
    entry_index: int = 0,
    capital_krw: float,
    config: BacktestConfig | None = None,
) -> SheetBacktestResult:
    """단일 시트를 일봉 시퀀스로 시뮬레이션.

    Args:
        sheet: 대상 PositionSheet (v1.1 스키마).
        bars: 시트 ticker 의 일봉 시퀀스, 오름차순. `bars[entry_index]` 를 진입 후보일로 사용.
            bars[:entry_index] 는 MA 부트스트랩용 과거 closes 로 쓰인다.
        entry_index: 기본 0. death_cross 같은 일봉 MA rule 이 있으면 caller 가
            ma_long 일 이상 prior bar 를 함께 전달해야 초반부터 동작한다.
        capital_krw: 이 시트에 배분된 자본. 수량 = int(capital / entry_price).
        config: 수수료/슬리피지. None 이면 기본값.
    """
    cfg = config or BacktestConfig()

    if not bars or entry_index >= len(bars) or entry_index < 0:
        return SheetBacktestResult(
            sheet_id=sheet.sheet_id,
            ticker=sheet.ticker,
            strategy_tag=sheet.strategy_tag,
            filled=False,
            trades=[],
            exit_reason="no_bars",
        )

    prior_bars = bars[:entry_index]
    entry_bar = bars[entry_index]

    # 1차 — base slippage 로 진입가 시도하여 수량을 추정.
    raw_entry_price, _, raw_reason = _try_entry(sheet, entry_bar, prior_bars, cfg, qty_hint=0)
    if raw_entry_price is None:
        return SheetBacktestResult(
            sheet_id=sheet.sheet_id,
            ticker=sheet.ticker,
            strategy_tag=sheet.strategy_tag,
            filled=False,
            trades=[],
            exit_reason=raw_reason,
        )
    qty_hint = int(capital_krw // raw_entry_price)
    if qty_hint <= 0:
        return SheetBacktestResult(
            sheet_id=sheet.sheet_id,
            ticker=sheet.ticker,
            strategy_tag=sheet.strategy_tag,
            filled=False,
            trades=[],
            exit_reason="capital_too_small",
        )

    # 2차 — qty_hint 로 volume-aware 진입가 재계산. volume_based_slippage 비활성 시
    # 1차/2차 결과 동일.
    entry_price, entry_ts, entry_reason = _try_entry(
        sheet, entry_bar, prior_bars, cfg, qty_hint=qty_hint
    )
    if entry_price is None or entry_ts is None:
        return SheetBacktestResult(
            sheet_id=sheet.sheet_id,
            ticker=sheet.ticker,
            strategy_tag=sheet.strategy_tag,
            filled=False,
            trades=[],
            exit_reason=entry_reason,
        )

    qty = int(capital_krw // entry_price)
    if qty <= 0:
        return SheetBacktestResult(
            sheet_id=sheet.sheet_id,
            ticker=sheet.ticker,
            strategy_tag=sheet.strategy_tag,
            filled=False,
            trades=[],
            exit_reason="capital_too_small",
        )

    buy_fee = entry_price * qty * cfg.buy_fee_pct / 100.0
    buy_trade = Trade(
        sheet_id=sheet.sheet_id,
        ticker=sheet.ticker,
        side="buy",
        qty=qty,
        price=entry_price,
        fee=buy_fee,
        ts=entry_ts,
        reason=entry_reason,
    )
    trades: list[Trade] = [buy_trade]

    state = PositionState(
        sheet_id=sheet.sheet_id,
        ticker=sheet.ticker,
        entry_price=entry_price,
        quantity=qty,
        entered_at=entry_ts,
    )

    # 평가용 일봉 종가 — prior + entry_bar 종가를 MA 부트스트랩으로 먼저 채운다
    daily_closes: list[float] = [b.close for b in prior_bars] + [entry_bar.close]
    remaining_qty = qty
    gross_pnl = 0.0
    total_fees = buy_fee
    last_exit_reason: str | None = None
    exit_ts: datetime | None = None

    for i, bar in enumerate(bars[entry_index + 1 :], start=1):
        ticks = _day_ticks(bar)
        closes_today = daily_closes + [bar.close]

        for t in ticks:
            if remaining_qty <= 0:
                break
            # death_cross 는 일봉 rule — close tick 에서만 daily_closes 노출
            closes_for_eval = closes_today if t.is_close else None
            tick = TickData(ticker=sheet.ticker, price=t.price, ts=t.ts)
            decision = evaluate(
                sheet,
                state,
                tick,
                now=t.ts,
                daily_closes=closes_for_eval,
                entered_business_days=i,
            )
            if decision is None:
                continue
            if not decision.should_close:
                # breakeven_sl_raise — state.breakeven_sl_price 만 갱신. trade 없음.
                continue

            # 청산 수량 결정
            if decision.portion >= 1.0:
                sell_qty = remaining_qty
            else:
                sell_qty = max(1, int(qty * decision.portion))
                sell_qty = min(sell_qty, remaining_qty)

            fill_price = _exit_fill_price(
                t.price, cfg, qty=sell_qty, bar_volume=bar.volume
            )
            fee = fill_price * sell_qty * cfg.sell_fee_pct / 100.0
            trades.append(
                Trade(
                    sheet_id=sheet.sheet_id,
                    ticker=sheet.ticker,
                    side="sell",
                    qty=sell_qty,
                    price=fill_price,
                    fee=fee,
                    ts=t.ts,
                    reason=decision.reason,
                    metadata=dict(decision.metadata),
                )
            )
            gross_pnl += (fill_price - entry_price) * sell_qty
            total_fees += fee
            remaining_qty -= sell_qty
            last_exit_reason = decision.reason
            exit_ts = t.ts

        daily_closes.append(bar.close)
        if remaining_qty <= 0:
            break

    # 데이터 끝까지 보유 상태면 마지막 close 로 강제 청산
    if remaining_qty > 0:
        last_bar = bars[-1]
        ts = datetime.combine(last_bar.price_date, _CLOSE_TIME)
        fill_price = _exit_fill_price(
            last_bar.close, cfg, qty=remaining_qty, bar_volume=last_bar.volume
        )
        fee = fill_price * remaining_qty * cfg.sell_fee_pct / 100.0
        trades.append(
            Trade(
                sheet_id=sheet.sheet_id,
                ticker=sheet.ticker,
                side="sell",
                qty=remaining_qty,
                price=fill_price,
                fee=fee,
                ts=ts,
                reason="data_end",
            )
        )
        gross_pnl += (fill_price - entry_price) * remaining_qty
        total_fees += fee
        last_exit_reason = "data_end"
        exit_ts = ts

    net_pnl = gross_pnl - total_fees
    pnl_pct = 0.0
    if entry_price > 0 and qty > 0:
        pnl_pct = net_pnl / (entry_price * qty) * 100.0

    return SheetBacktestResult(
        sheet_id=sheet.sheet_id,
        ticker=sheet.ticker,
        strategy_tag=sheet.strategy_tag,
        filled=True,
        trades=trades,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        entry_price=entry_price,
        total_qty=qty,
        gross_pnl_krw=gross_pnl,
        fees_krw=total_fees,
        net_pnl_krw=net_pnl,
        pnl_pct=pnl_pct,
        exit_reason=last_exit_reason,
    )


__all__ = ["simulate_sheet"]


# 미사용 import 방지 — 위 로직에서 date 를 명시 참조하진 않지만 타입 힌트 완결성을 위해
_ = date
