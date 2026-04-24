"""`summarize` 가 SheetBacktestResult 리스트를 올바르게 집계하는지."""

from __future__ import annotations

from datetime import datetime

from prime_jennie_runtime.backtest import (
    SheetBacktestResult,
    Trade,
    format_report,
    summarize,
)


def _result(
    sheet_id: str,
    *,
    filled: bool = True,
    pnl_pct: float = 0.0,
    strategy_tag: str = "GAP_UP_REBOUND",
    exit_reason: str | None = "time_stop",
    ticker: str = "005930",
) -> SheetBacktestResult:
    return SheetBacktestResult(
        sheet_id=sheet_id,
        ticker=ticker,
        strategy_tag=strategy_tag,
        filled=filled,
        trades=[
            Trade(
                sheet_id=sheet_id,
                ticker=ticker,
                side="buy",
                qty=1,
                price=10000.0,
                fee=1.5,
                ts=datetime(2026, 5, 4, 9),
                reason="market",
            )
        ]
        if filled
        else [],
        entry_ts=datetime(2026, 5, 4, 9) if filled else None,
        exit_ts=datetime(2026, 5, 4, 15, 30) if filled else None,
        entry_price=10000.0 if filled else 0.0,
        total_qty=1 if filled else 0,
        gross_pnl_krw=pnl_pct * 100.0,
        fees_krw=2.0 if filled else 0.0,
        net_pnl_krw=pnl_pct * 100.0 - (2.0 if filled else 0.0),
        pnl_pct=pnl_pct,
        exit_reason=exit_reason,
    )


def test_summarize_empty():
    s = summarize([])
    assert s.n_total == 0
    assert s.n_filled == 0
    assert s.hit_rate == 0.0


def test_summarize_basic_filled_distribution():
    results = [
        _result("ps_1", pnl_pct=+3.0, exit_reason="fixed_tp"),
        _result("ps_2", pnl_pct=-2.0, exit_reason="fixed_sl"),
        _result("ps_3", pnl_pct=+1.5, exit_reason="time_stop"),
        _result("ps_4", pnl_pct=-0.5, exit_reason="time_stop"),
    ]
    s = summarize(results)
    assert s.n_total == 4
    assert s.n_filled == 4
    assert s.n_unfilled == 0
    assert s.hit_rate == 0.5
    assert s.avg_pnl_pct == (3.0 - 2.0 + 1.5 - 0.5) / 4
    assert s.std_pnl_pct > 0
    assert "time_stop" in s.by_exit_reason
    assert s.by_exit_reason["time_stop"].count == 2
    assert s.by_strategy_tag["GAP_UP_REBOUND"].count == 4


def test_summarize_tracks_unfilled():
    results = [
        _result("ps_1", pnl_pct=+2.0),
        _result("ps_2", filled=False, exit_reason="limit_unfilled"),
        _result("ps_3", filled=False, exit_reason="limit_unfilled"),
        _result("ps_4", filled=False, exit_reason="condition_failed"),
    ]
    s = summarize(results)
    assert s.n_filled == 1
    assert s.n_unfilled == 3
    assert s.by_unfilled_reason["limit_unfilled"] == 2
    assert s.by_unfilled_reason["condition_failed"] == 1


def test_format_report_runs_without_error():
    results = [_result("ps_1", pnl_pct=+2.0)]
    txt = format_report(summarize(results))
    assert "Backtest Summary" in txt
    assert "time_stop" in txt
