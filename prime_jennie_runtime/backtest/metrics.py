"""성과 지표 — 총수익률, MDD, Sharpe, 전략별·매도사유별 분석 + 리포트 출력.

포팅 원본: prime-jennie/prime_jennie/services/backtest/metrics.py
v2 와 1:1. 출력 포맷 (BacktestMetrics, CSV 컬럼) 은 dashboard 참조용 안정 보증.
"""

from __future__ import annotations

import csv
import math
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .models import DailySnapshot, TradeLog


@dataclass
class BacktestMetrics:
    """백테스트 성과 요약."""

    # 기본 성과
    initial_capital: int = 0
    final_value: int = 0
    total_return_pct: float = 0.0
    annualized_return_pct: float = 0.0
    trading_days: int = 0

    # 리스크
    max_drawdown_pct: float = 0.0
    max_drawdown_start: date | None = None
    max_drawdown_end: date | None = None

    # 거래 통계
    total_trades: int = 0
    total_buys: int = 0
    total_sells: int = 0
    win_count: int = 0
    loss_count: int = 0
    win_rate_pct: float = 0.0
    profit_factor: float = 0.0
    avg_profit_pct: float = 0.0
    avg_loss_pct: float = 0.0
    avg_holding_days: float = 0.0
    max_profit_pct: float = 0.0
    max_loss_pct: float = 0.0

    # 비율 지표
    sharpe_ratio: float = 0.0
    total_fees: int = 0

    # 상세 분석
    strategy_stats: dict[str, dict] = field(default_factory=dict)
    exit_reason_stats: dict[str, dict] = field(default_factory=dict)


def calculate_metrics(
    snapshots: list[DailySnapshot],
    trade_logs: list[TradeLog],
    initial_capital: int,
) -> BacktestMetrics:
    """스냅샷과 거래 로그에서 성과 지표 계산."""
    m = BacktestMetrics()
    m.initial_capital = initial_capital
    m.trading_days = len(snapshots)

    if not snapshots:
        return m

    # --- 기본 성과 ---
    m.final_value = snapshots[-1].total_value
    m.total_return_pct = (m.final_value - initial_capital) / initial_capital * 100

    if m.trading_days > 0:
        years = m.trading_days / 252  # 한국 거래일 기준
        if years > 0:
            ratio = m.final_value / initial_capital
            if ratio > 0:
                m.annualized_return_pct = (ratio ** (1 / years) - 1) * 100

    # --- MDD ---
    peak = initial_capital
    max_dd = 0.0
    dd_start: date | None = None
    curr_dd_start: date | None = None

    for snap in snapshots:
        if snap.total_value > peak:
            peak = snap.total_value
            curr_dd_start = snap.snapshot_date
        dd = (peak - snap.total_value) / peak * 100
        if dd > max_dd:
            max_dd = dd
            dd_start = curr_dd_start
            m.max_drawdown_end = snap.snapshot_date

    m.max_drawdown_pct = max_dd
    m.max_drawdown_start = dd_start

    # --- 거래 통계 ---
    sells = [t for t in trade_logs if t.trade_type == "SELL"]
    buys = [t for t in trade_logs if t.trade_type == "BUY"]
    m.total_buys = len(buys)
    m.total_sells = len(sells)
    m.total_trades = m.total_buys + m.total_sells
    m.total_fees = sum(t.fee for t in trade_logs)

    if sells:
        profits = [t.profit_pct for t in sells if t.profit_pct is not None]
        winners = [p for p in profits if p > 0]
        losers = [p for p in profits if p <= 0]

        m.win_count = len(winners)
        m.loss_count = len(losers)
        m.win_rate_pct = m.win_count / len(profits) * 100 if profits else 0

        m.avg_profit_pct = sum(winners) / len(winners) if winners else 0
        m.avg_loss_pct = sum(losers) / len(losers) if losers else 0
        m.max_profit_pct = max(profits) if profits else 0
        m.max_loss_pct = min(profits) if profits else 0

        gross_profit = sum(
            t.profit_amount for t in sells if t.profit_amount and t.profit_amount > 0
        )
        gross_loss = abs(
            sum(t.profit_amount for t in sells if t.profit_amount and t.profit_amount < 0)
        )
        m.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        holding_days = [t.holding_days for t in sells if t.holding_days is not None]
        m.avg_holding_days = sum(holding_days) / len(holding_days) if holding_days else 0

    # --- Sharpe Ratio ---
    if len(snapshots) >= 2:
        daily_returns = [s.daily_return_pct for s in snapshots]
        avg_ret = sum(daily_returns) / len(daily_returns)
        if len(daily_returns) > 1:
            variance = sum((r - avg_ret) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
            std_ret = math.sqrt(variance) if variance > 0 else 0
            if std_ret > 0:
                m.sharpe_ratio = (avg_ret / std_ret) * math.sqrt(252)

    m.strategy_stats = _analyze_by_strategy(sells)
    m.exit_reason_stats = _analyze_by_exit_reason(sells)

    return m


def print_report(m: BacktestMetrics) -> None:
    """콘솔 리포트 출력."""
    sep = "=" * 60

    print(f"\n{sep}")
    print("  BACKTEST REPORT")
    print(sep)

    print(f"\n{'Initial Capital':<25} {m.initial_capital:>15,} KRW")
    print(f"{'Final Value':<25} {m.final_value:>15,} KRW")
    print(f"{'Total Return':<25} {m.total_return_pct:>14.2f}%")
    print(f"{'Annualized Return':<25} {m.annualized_return_pct:>14.2f}%")
    print(f"{'Max Drawdown':<25} {m.max_drawdown_pct:>14.2f}%")
    if m.max_drawdown_start and m.max_drawdown_end:
        print(f"{'  MDD Period':<25} {m.max_drawdown_start} ~ {m.max_drawdown_end}")
    print(f"{'Sharpe Ratio':<25} {m.sharpe_ratio:>14.2f}")
    print(f"{'Trading Days':<25} {m.trading_days:>15d}")
    print(f"{'Total Fees':<25} {m.total_fees:>15,} KRW")

    print(f"\n{'-' * 60}")
    print("  TRADE STATISTICS")
    print(f"{'-' * 60}")
    print(f"{'Total Buys':<25} {m.total_buys:>15d}")
    print(f"{'Total Sells':<25} {m.total_sells:>15d}")
    print(f"{'Win Rate':<25} {m.win_rate_pct:>14.1f}%")
    print(f"{'Wins / Losses':<25} {m.win_count:>7d} / {m.loss_count}")
    print(f"{'Profit Factor':<25} {m.profit_factor:>14.2f}")
    print(f"{'Avg Profit (wins)':<25} {m.avg_profit_pct:>14.2f}%")
    print(f"{'Avg Loss (losses)':<25} {m.avg_loss_pct:>14.2f}%")
    print(f"{'Max Profit':<25} {m.max_profit_pct:>14.2f}%")
    print(f"{'Max Loss':<25} {m.max_loss_pct:>14.2f}%")
    print(f"{'Avg Holding Days':<25} {m.avg_holding_days:>14.1f}")

    if m.strategy_stats:
        print(f"\n{'-' * 60}")
        print("  BY STRATEGY")
        print(f"{'-' * 60}")
        print(f"  {'Strategy':<25} {'Count':>6} {'Win%':>7} {'AvgPnL':>8}")
        for name, s in sorted(m.strategy_stats.items(), key=lambda x: x[1]["count"], reverse=True):
            print(f"  {name:<25} {s['count']:>6} {s['win_rate']:>6.1f}% {s['avg_pnl']:>7.2f}%")

    if m.exit_reason_stats:
        print(f"\n{'-' * 60}")
        print("  BY EXIT REASON")
        print(f"{'-' * 60}")
        print(f"  {'Reason':<25} {'Count':>6} {'Win%':>7} {'AvgPnL':>8}")
        for name, s in sorted(
            m.exit_reason_stats.items(), key=lambda x: x[1]["count"], reverse=True
        ):
            print(f"  {name:<25} {s['count']:>6} {s['win_rate']:>6.1f}% {s['avg_pnl']:>7.2f}%")

    print(f"\n{sep}\n")


def export_csv(
    trade_logs: list[TradeLog],
    snapshots: list[DailySnapshot],
    output_dir: str,
) -> None:
    """거래 로그와 스냅샷을 CSV로 내보내기."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    trades_path = os.path.join(output_dir, "trades.csv")
    with open(trades_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "date",
                "stock_code",
                "stock_name",
                "type",
                "quantity",
                "price",
                "total_amount",
                "fee",
                "signal",
                "tier",
                "sell_reason",
                "profit_pct",
                "profit_amount",
                "holding_days",
                "regime",
            ]
        )
        for t in trade_logs:
            writer.writerow(
                [
                    t.trade_date,
                    t.stock_code,
                    t.stock_name,
                    t.trade_type,
                    t.quantity,
                    t.price,
                    t.total_amount,
                    t.fee,
                    t.signal_type or "",
                    t.trade_tier or "",
                    t.sell_reason or "",
                    f"{t.profit_pct:.2f}" if t.profit_pct is not None else "",
                    t.profit_amount or "",
                    t.holding_days or "",
                    t.regime or "",
                ]
            )

    snapshots_path = os.path.join(output_dir, "daily_snapshots.csv")
    with open(snapshots_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "date",
                "cash",
                "portfolio_value",
                "total_value",
                "position_count",
                "daily_return_pct",
                "regime",
            ]
        )
        for s in snapshots:
            writer.writerow(
                [
                    s.snapshot_date,
                    s.cash,
                    s.portfolio_value,
                    s.total_value,
                    s.position_count,
                    f"{s.daily_return_pct:.4f}",
                    s.regime,
                ]
            )

    print(f"Exported: {trades_path}")
    print(f"Exported: {snapshots_path}")


# --- Internal helpers ---


def _analyze_by_strategy(sells: list[TradeLog]) -> dict[str, dict]:
    """전략별 성과 분석."""
    groups: dict[str, list[TradeLog]] = defaultdict(list)
    for t in sells:
        key = str(t.signal_type) if t.signal_type else "UNKNOWN"
        groups[key].append(t)

    result = {}
    for name, trades in groups.items():
        profits = [t.profit_pct for t in trades if t.profit_pct is not None]
        wins = [p for p in profits if p > 0]
        result[name] = {
            "count": len(trades),
            "win_rate": len(wins) / len(profits) * 100 if profits else 0,
            "avg_pnl": sum(profits) / len(profits) if profits else 0,
            "total_pnl": sum(t.profit_amount for t in trades if t.profit_amount),
        }
    return result


def _analyze_by_exit_reason(sells: list[TradeLog]) -> dict[str, dict]:
    """매도 사유별 성과 분석."""
    groups: dict[str, list[TradeLog]] = defaultdict(list)
    for t in sells:
        key = str(t.sell_reason) if t.sell_reason else "UNKNOWN"
        groups[key].append(t)

    result = {}
    for name, trades in groups.items():
        profits = [t.profit_pct for t in trades if t.profit_pct is not None]
        wins = [p for p in profits if p > 0]
        result[name] = {
            "count": len(trades),
            "win_rate": len(wins) / len(profits) * 100 if profits else 0,
            "avg_pnl": sum(profits) / len(profits) if profits else 0,
            "total_pnl": sum(t.profit_amount for t in trades if t.profit_amount),
        }
    return result


__all__ = [
    "BacktestMetrics",
    "calculate_metrics",
    "export_csv",
    "print_report",
]
