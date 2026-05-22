"""compute_outcome — executions 행 → outcomes 집계 단위 테스트.

운영 청산 경로(record_sell, fully_closed=True)와 과거분 backfill 이 공유하는
순수 함수. entry/exit 가중평균·거래비용 차감·exit_reason 추출을 검증.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from prime_jennie_runtime.fast_loop.persistence import (
    _BUY_FEE_RATE,
    _SELL_FEE_RATE,
    compute_outcome,
)

_T0 = datetime(2026, 5, 20, 0, 30, 0, tzinfo=UTC)


def _row(
    side: str,
    price: float,
    qty: int,
    *,
    offset_s: int = 0,
    exit_reason: str | None = None,
    meta_as_dict: bool = False,
) -> dict:
    """executions 한 행 모킹. metadata_json 은 기본 JSON 문자열 (asyncpg jsonb)."""
    meta: dict = {}
    if exit_reason is not None:
        meta["exit_reason"] = exit_reason
    metadata_json: object | None
    if not meta:
        metadata_json = None
    elif meta_as_dict:
        metadata_json = meta
    else:
        metadata_json = json.dumps(meta)
    return {
        "side": side,
        "price": price,
        "qty": qty,
        "executed_at": _T0 + timedelta(seconds=offset_s),
        "metadata_json": metadata_json,
    }


def test_single_buy_single_sell_profit():
    rows = [
        _row("buy", 70000, 10, offset_s=0),
        _row("sell", 73000, 10, offset_s=3600, exit_reason="fixed_tp"),
    ]
    out = compute_outcome(rows)
    assert out is not None
    assert out.entry_price == pytest.approx(70000.0)
    assert out.exit_price == pytest.approx(73000.0)
    assert out.matched_qty == 10
    assert out.gross_pnl_krw == pytest.approx(30000.0)
    expected_fees = 70000 * 10 * _BUY_FEE_RATE + 73000 * 10 * _SELL_FEE_RATE
    assert out.fees_krw == pytest.approx(expected_fees)
    assert out.pnl_krw == pytest.approx(30000.0 - expected_fees)
    assert out.pnl_pct == pytest.approx(out.pnl_krw / 700000.0 * 100.0)
    assert out.holding_period_s == 3600
    assert out.closed_at == _T0 + timedelta(seconds=3600)
    assert out.exit_reason == "fixed_tp"


def test_single_buy_single_sell_loss():
    rows = [
        _row("buy", 70000, 10, offset_s=0),
        _row("sell", 66500, 10, offset_s=600, exit_reason="fixed_sl"),
    ]
    out = compute_outcome(rows)
    assert out is not None
    assert out.gross_pnl_krw == pytest.approx(-35000.0)
    # 손실 + 거래비용 → net 손실은 gross 보다 더 크다
    assert out.pnl_krw < -35000.0
    assert out.pnl_pct < 0
    assert out.exit_reason == "fixed_sl"


def test_scale_out_weighted_exit_price():
    """부분 매도 여러 건 — exit_price 는 매도 수량 가중평균, exit_reason 은 마지막 매도."""
    rows = [
        _row("buy", 100, 100, offset_s=0),
        _row("sell", 110, 50, offset_s=100, exit_reason="scale_out"),
        _row("sell", 120, 50, offset_s=200, exit_reason="time_stop"),
    ]
    out = compute_outcome(rows)
    assert out is not None
    assert out.entry_price == pytest.approx(100.0)
    assert out.exit_price == pytest.approx(115.0)  # (110*50 + 120*50)/100
    assert out.matched_qty == 100
    assert out.gross_pnl_krw == pytest.approx(1500.0)
    assert out.exit_reason == "time_stop"  # 마지막 매도
    assert out.closed_at == _T0 + timedelta(seconds=200)


def test_multiple_buys_weighted_entry_price():
    rows = [
        _row("buy", 90, 10, offset_s=0),
        _row("buy", 110, 10, offset_s=50),
        _row("sell", 120, 20, offset_s=300, exit_reason="trailing_tp"),
    ]
    out = compute_outcome(rows)
    assert out is not None
    assert out.entry_price == pytest.approx(100.0)  # (90*10 + 110*10)/20
    assert out.exit_price == pytest.approx(120.0)
    assert out.holding_period_s == 300  # 첫 매수 → 마지막 매도


def test_no_sell_returns_none():
    assert compute_outcome([_row("buy", 70000, 10)]) is None


def test_no_buy_returns_none():
    assert compute_outcome([_row("sell", 70000, 10, exit_reason="manual")]) is None


def test_empty_returns_none():
    assert compute_outcome([]) is None


def test_exit_reason_from_dict_metadata():
    """metadata_json 이 (codec 등록 시) dict 로 와도 exit_reason 추출."""
    rows = [
        _row("buy", 100, 1, offset_s=0),
        _row("sell", 105, 1, offset_s=60, exit_reason="breakeven", meta_as_dict=True),
    ]
    out = compute_outcome(rows)
    assert out is not None
    assert out.exit_reason == "breakeven"


def test_exit_reason_none_when_metadata_absent():
    rows = [
        _row("buy", 100, 1, offset_s=0),
        _row("sell", 105, 1, offset_s=60),  # exit_reason 없음
    ]
    out = compute_outcome(rows)
    assert out is not None
    assert out.exit_reason is None


def test_matched_qty_uses_min_when_quantities_differ():
    """매수/매도 수량이 어긋나면 matched_qty 는 작은 쪽 (방어적)."""
    rows = [
        _row("buy", 100, 10, offset_s=0),
        _row("sell", 110, 8, offset_s=60, exit_reason="fixed_tp"),
    ]
    out = compute_outcome(rows)
    assert out is not None
    assert out.matched_qty == 8
    assert out.gross_pnl_krw == pytest.approx((110 - 100) * 8)
