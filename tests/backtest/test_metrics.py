"""calculate_metrics + export_csv 검증."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from prime_jennie_runtime.backtest import (
    DailySnapshot,
    MarketRegime,
    SellReason,
    SignalType,
    TradeLog,
    calculate_metrics,
    export_csv,
)


def _snap(d: date, tv: int, ret: float = 0.0) -> DailySnapshot:
    return DailySnapshot(
        snapshot_date=d,
        cash=tv,
        portfolio_value=0,
        total_value=tv,
        position_count=0,
        daily_return_pct=ret,
        regime=MarketRegime.SIDEWAYS,
    )


def _trade(
    d: date,
    *,
    side: str,
    profit_pct: float | None = None,
    profit_amount: int | None = None,
    signal: SignalType | None = SignalType.MOMENTUM,
    reason: SellReason | None = None,
    fee: int = 0,
    holding_days: int | None = None,
) -> TradeLog:
    return TradeLog(
        trade_date=d,
        stock_code="A",
        stock_name="A",
        trade_type=side,
        quantity=1,
        price=1000,
        total_amount=1000,
        fee=fee,
        signal_type=signal,
        sell_reason=reason,
        profit_pct=profit_pct,
        profit_amount=profit_amount,
        holding_days=holding_days,
    )


def test_calculate_metrics_empty_snapshots():
    m = calculate_metrics([], [], 10_000_000)
    assert m.initial_capital == 10_000_000
    assert m.trading_days == 0
    assert m.final_value == 0


def test_calculate_metrics_total_and_annualized_return():
    snapshots = [
        _snap(date(2026, 1, 2), 10_000_000),
        _snap(date(2026, 7, 1), 11_000_000),
    ]
    m = calculate_metrics(snapshots, [], 10_000_000)
    assert m.trading_days == 2
    assert m.final_value == 11_000_000
    assert m.total_return_pct == 10.0
    # 연환산: 11_000_000/10_000_000 = 1.1, 2/252년 기준 매우 높게 나옴
    assert m.annualized_return_pct > 100


def test_calculate_metrics_mdd_tracks_peak_drawdown():
    snapshots = [
        _snap(date(2026, 1, 1), 10_000_000),
        _snap(date(2026, 2, 1), 12_000_000),  # peak
        _snap(date(2026, 3, 1), 9_000_000),  # -25% from peak
        _snap(date(2026, 4, 1), 11_000_000),  # partial recovery
    ]
    m = calculate_metrics(snapshots, [], 10_000_000)
    assert m.max_drawdown_pct == 25.0
    assert m.max_drawdown_start == date(2026, 2, 1)
    assert m.max_drawdown_end == date(2026, 3, 1)


def test_calculate_metrics_win_rate_and_profit_factor():
    sells = [
        _trade(
            date(2026, 4, 1),
            side="SELL",
            profit_pct=5.0,
            profit_amount=500,
            reason=SellReason.PROFIT_TARGET,
            holding_days=3,
        ),
        _trade(
            date(2026, 4, 2),
            side="SELL",
            profit_pct=-2.0,
            profit_amount=-200,
            reason=SellReason.STOP_LOSS,
            holding_days=1,
        ),
        _trade(
            date(2026, 4, 3),
            side="SELL",
            profit_pct=10.0,
            profit_amount=1000,
            reason=SellReason.PROFIT_TARGET,
            holding_days=5,
        ),
    ]
    buys = [_trade(date(2026, 4, 1), side="BUY", signal=SignalType.MOMENTUM)]
    m = calculate_metrics(
        [_snap(date(2026, 4, 1), 10_000_000), _snap(date(2026, 4, 5), 10_500_000)],
        sells + buys,
        10_000_000,
    )
    assert m.total_buys == 1
    assert m.total_sells == 3
    assert m.win_count == 2
    assert m.loss_count == 1
    assert abs(m.win_rate_pct - (2 / 3 * 100)) < 1e-6
    # profit_factor = 1500 / 200 = 7.5
    assert m.profit_factor == 7.5
    assert m.avg_profit_pct == 7.5
    assert m.avg_loss_pct == -2.0
    assert m.max_profit_pct == 10.0
    assert m.max_loss_pct == -2.0
    assert m.avg_holding_days == 3.0


def test_calculate_metrics_profit_factor_infinite_when_no_losses():
    sells = [
        _trade(
            date(2026, 4, 1),
            side="SELL",
            profit_pct=5.0,
            profit_amount=500,
            reason=SellReason.PROFIT_TARGET,
            holding_days=1,
        ),
    ]
    m = calculate_metrics([_snap(date(2026, 4, 1), 10_000_000)], sells, 10_000_000)
    assert m.profit_factor == float("inf")


def test_calculate_metrics_strategy_stats_groups_by_signal():
    sells = [
        _trade(
            date(2026, 4, 1),
            side="SELL",
            profit_pct=3.0,
            profit_amount=300,
            signal=SignalType.MOMENTUM,
            reason=SellReason.PROFIT_TARGET,
            holding_days=2,
        ),
        _trade(
            date(2026, 4, 2),
            side="SELL",
            profit_pct=-1.0,
            profit_amount=-100,
            signal=SignalType.DIP_BUY,
            reason=SellReason.STOP_LOSS,
            holding_days=1,
        ),
    ]
    m = calculate_metrics([_snap(date(2026, 4, 1), 10_000_000)], sells, 10_000_000)
    assert "SignalType.MOMENTUM" in str(m.strategy_stats) or "MOMENTUM" in m.strategy_stats
    momentum_key = next(k for k in m.strategy_stats if "MOMENTUM" in k)
    dip_key = next(k for k in m.strategy_stats if "DIP_BUY" in k)
    assert m.strategy_stats[momentum_key]["count"] == 1
    assert m.strategy_stats[dip_key]["count"] == 1


def test_export_csv_writes_files_with_expected_columns(tmp_path: Path):
    trades = [
        _trade(
            date(2026, 4, 1),
            side="BUY",
            fee=10,
            signal=SignalType.MOMENTUM,
            holding_days=None,
        ),
        _trade(
            date(2026, 4, 5),
            side="SELL",
            profit_pct=5.0,
            profit_amount=500,
            reason=SellReason.PROFIT_TARGET,
            fee=20,
            holding_days=4,
        ),
    ]
    snapshots = [_snap(date(2026, 4, 1), 10_000_000, ret=0.1)]
    export_csv(trades, snapshots, str(tmp_path))

    trades_path = tmp_path / "trades.csv"
    snapshots_path = tmp_path / "daily_snapshots.csv"
    assert trades_path.exists()
    assert snapshots_path.exists()

    trades_text = trades_path.read_text(encoding="utf-8")
    # 헤더 안정성 (dashboard 참조용)
    assert trades_text.splitlines()[0] == (
        "date,stock_code,stock_name,type,quantity,price,total_amount,fee,"
        "signal,tier,sell_reason,profit_pct,profit_amount,holding_days,regime"
    )
    snapshots_text = snapshots_path.read_text(encoding="utf-8")
    assert snapshots_text.splitlines()[0] == (
        "date,cash,portfolio_value,total_value,position_count,daily_return_pct,regime"
    )
