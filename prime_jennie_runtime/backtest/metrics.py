"""시트 단위 백테스트 결과 집계.

Portfolio 단위 MDD/Sharpe 는 시트가 중첩되기 때문에 별도 equity curve 구축이
필요 — v1 에선 시트 단위 분포만 집계한다. 포트폴리오 레벨은 driver 가
별도 합성한다.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from .domain import SheetBacktestResult


@dataclass
class ReasonStats:
    count: int = 0
    avg_pnl_pct: float = 0.0
    total_net_pnl_krw: float = 0.0


@dataclass
class BacktestSummary:
    """다수 시트 결과 집계."""

    n_total: int = 0
    n_filled: int = 0
    n_unfilled: int = 0

    avg_pnl_pct: float = 0.0  # filled 시트 평균
    median_pnl_pct: float = 0.0
    std_pnl_pct: float = 0.0
    hit_rate: float = 0.0  # filled 중 pnl_pct > 0 비율
    sharpe_like: float = 0.0  # 시트별 pnl_pct 로 산출한 mean/std (엄밀한 Sharpe 아님)

    total_gross_pnl_krw: float = 0.0
    total_fees_krw: float = 0.0
    total_net_pnl_krw: float = 0.0

    by_exit_reason: dict[str, ReasonStats] = field(default_factory=dict)
    by_strategy_tag: dict[str, ReasonStats] = field(default_factory=dict)
    by_unfilled_reason: dict[str, int] = field(default_factory=dict)


def _stats_for_subset(results: list[SheetBacktestResult]) -> ReasonStats:
    if not results:
        return ReasonStats()
    total_pnl = sum(r.net_pnl_krw for r in results)
    avg_pct = sum(r.pnl_pct for r in results) / len(results)
    return ReasonStats(
        count=len(results),
        avg_pnl_pct=avg_pct,
        total_net_pnl_krw=total_pnl,
    )


def summarize(results: list[SheetBacktestResult]) -> BacktestSummary:
    """시트 결과 리스트 → 전체 집계."""
    summary = BacktestSummary(n_total=len(results))

    filled = [r for r in results if r.filled]
    unfilled = [r for r in results if not r.filled]
    summary.n_filled = len(filled)
    summary.n_unfilled = len(unfilled)

    for r in unfilled:
        reason = r.exit_reason or "unknown"
        summary.by_unfilled_reason[reason] = summary.by_unfilled_reason.get(reason, 0) + 1

    if not filled:
        return summary

    pnl_pcts = sorted(r.pnl_pct for r in filled)
    summary.avg_pnl_pct = sum(pnl_pcts) / len(pnl_pcts)
    summary.median_pnl_pct = pnl_pcts[len(pnl_pcts) // 2]
    summary.hit_rate = sum(1 for p in pnl_pcts if p > 0) / len(pnl_pcts)

    if len(pnl_pcts) > 1:
        mean = summary.avg_pnl_pct
        var = sum((p - mean) ** 2 for p in pnl_pcts) / (len(pnl_pcts) - 1)
        summary.std_pnl_pct = math.sqrt(var)
        if summary.std_pnl_pct > 0:
            summary.sharpe_like = summary.avg_pnl_pct / summary.std_pnl_pct

    summary.total_gross_pnl_krw = sum(r.gross_pnl_krw for r in filled)
    summary.total_fees_krw = sum(r.fees_krw for r in filled)
    summary.total_net_pnl_krw = sum(r.net_pnl_krw for r in filled)

    by_reason: dict[str, list[SheetBacktestResult]] = defaultdict(list)
    by_tag: dict[str, list[SheetBacktestResult]] = defaultdict(list)
    for r in filled:
        by_reason[r.exit_reason or "unknown"].append(r)
        by_tag[r.strategy_tag].append(r)

    summary.by_exit_reason = {k: _stats_for_subset(v) for k, v in by_reason.items()}
    summary.by_strategy_tag = {k: _stats_for_subset(v) for k, v in by_tag.items()}
    return summary


def format_report(summary: BacktestSummary) -> str:
    """콘솔/로그 친화 텍스트 리포트."""
    lines = [
        "=== Backtest Summary ===",
        f"sheets: {summary.n_total} (filled={summary.n_filled}, unfilled={summary.n_unfilled})",
    ]
    if summary.n_filled:
        lines += [
            f"avg pnl: {summary.avg_pnl_pct:+.2f}%  median: {summary.median_pnl_pct:+.2f}%  "
            f"std: {summary.std_pnl_pct:.2f}%  sharpe-like: {summary.sharpe_like:+.2f}",
            f"hit rate: {summary.hit_rate * 100:.1f}%",
            f"gross: {summary.total_gross_pnl_krw:+,.0f}  fees: {summary.total_fees_krw:,.0f}  "
            f"net: {summary.total_net_pnl_krw:+,.0f}",
        ]
        lines.append("")
        lines.append("-- by exit_reason --")
        for reason, s in sorted(summary.by_exit_reason.items(), key=lambda kv: -kv[1].count):
            lines.append(
                f"  {reason:<20} n={s.count:<4} avg={s.avg_pnl_pct:+.2f}%  "
                f"net={s.total_net_pnl_krw:+,.0f}"
            )
        lines.append("")
        lines.append("-- by strategy_tag --")
        for tag, s in sorted(summary.by_strategy_tag.items(), key=lambda kv: -kv[1].count):
            lines.append(
                f"  {tag:<25} n={s.count:<4} avg={s.avg_pnl_pct:+.2f}%  "
                f"net={s.total_net_pnl_krw:+,.0f}"
            )
    if summary.n_unfilled:
        lines.append("")
        lines.append("-- unfilled --")
        for reason, n in sorted(summary.by_unfilled_reason.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {reason:<20} n={n}")
    return "\n".join(lines)


__all__ = ["BacktestSummary", "ReasonStats", "format_report", "summarize"]
