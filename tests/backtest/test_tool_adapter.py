"""`BacktestToolAdapter` 테스트.

Adapter 는 ``simulate_sheet`` 의 얇은 래퍼이므로 exit-rule 단위 검증은
``test_runner`` 가 맡고, 여기선 다음만 검증:

* ToolAdapter 프로토콜 구조 (ducktype) 적합성
* PositionSheet 인스턴스/dict 둘 다 입력 허용
* fixed_sl / time_stop / scale_out 시나리오에서 exit_reason 이 올바르게 propagate
* schema validation 실패 시 raise 하지 않고 ToolResult(ok=False, ...) 로 반환
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from minyoung_mah import ErrorCategory, ToolAdapter, ToolResult

from prime_jennie_runtime.backtest import (
    BacktestToolAdapter,
    BacktestToolAdapterArgs,
    BacktestToolAdapterValue,
    DailyBar,
)
from prime_jennie_runtime.position_sheet.schema import (
    KST,
    EntrySection,
    ExitSection,
    FixedSlRule,
    MacroStateSnapshot,
    PositionSheet,
    ProvenanceSection,
    ScaleOutRule,
    SizeSection,
    TimeStopRule,
)

# ---------------------------------------------------------------------------
# Helpers — test_runner.py 와 동일한 합성 시트/일봉
# ---------------------------------------------------------------------------


def _mk_sheet(rules: list, *, generated_date: date = date(2026, 5, 4)) -> PositionSheet:
    generated_at = datetime.combine(generated_date, datetime.min.time(), tzinfo=KST).replace(
        hour=9, minute=0
    )
    valid_until = generated_at.replace(hour=15, minute=20)
    return PositionSheet(
        sheet_id=f"ps_{generated_date.strftime('%Y%m%d')}_090000_a1b2",
        schema_version="1.1",
        generated_at=generated_at,
        valid_until=valid_until,
        ticker="005930",
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
            trigger="market",
            price=None,
            valid_until=valid_until,
            conditions=[],
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


def _bar_dict(bar: DailyBar) -> dict:
    return {
        "ticker": bar.ticker,
        "price_date": bar.price_date.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
    }


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_adapter_satisfies_tooladapter_protocol():
    adapter = BacktestToolAdapter()
    # runtime_checkable Protocol — duck-type 검사
    assert isinstance(adapter, ToolAdapter)
    assert adapter.name == "backtest_simulate_sheet"
    assert isinstance(adapter.description, str) and adapter.description
    assert adapter.arg_schema is BacktestToolAdapterArgs


# ---------------------------------------------------------------------------
# Happy path — sheet 인스턴스 입력
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_with_position_sheet_instance_returns_filled_result():
    sheet = _mk_sheet([FixedSlRule(pct=0.05), TimeStopRule(mode="hold_days", value=10)])
    bars = _bars(date(2026, 5, 4), [(10_000, 10_100, 9_900, 10_000)] * 5)
    args = BacktestToolAdapterArgs(
        sheet=sheet,
        bars=[_bar_dict(b) for b in bars],
        capital_krw=1_000_000,
    )

    adapter = BacktestToolAdapter()
    result = await adapter.call(args)

    assert isinstance(result, ToolResult)
    assert result.ok is True
    assert result.error is None
    assert isinstance(result.value, BacktestToolAdapterValue)
    assert result.value.result.filled is True
    assert result.value.result.sheet_id == sheet.sheet_id
    assert result.value.summary.n_total == 1
    assert result.value.summary.n_filled == 1
    assert result.metadata["filled"] is True
    assert result.metadata["n_trades"] >= 2  # 매수 + (data_end 등) 매도 ≥1
    assert result.duration_ms >= 0


# ---------------------------------------------------------------------------
# Sheet as dict (JSON-네이티브 호출 경로)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_with_sheet_dict_validates_and_runs():
    sheet = _mk_sheet([FixedSlRule(pct=0.05), TimeStopRule(mode="eod")])
    sheet_dict = sheet.model_dump(mode="json")
    bars = _bars(date(2026, 5, 4), [(10_000, 10_100, 9_900, 10_000)] * 3)

    args = BacktestToolAdapterArgs(
        sheet=sheet_dict,
        bars=[_bar_dict(b) for b in bars],
        capital_krw=1_000_000,
    )

    adapter = BacktestToolAdapter()
    result = await adapter.call(args)

    assert result.ok is True
    assert result.value.result.filled is True
    assert result.value.result.exit_reason == "time_stop"


# ---------------------------------------------------------------------------
# fixed_sl trigger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fixed_sl_trigger_propagates_to_tool_result():
    sheet = _mk_sheet([FixedSlRule(pct=0.05), TimeStopRule(mode="hold_days", value=30)])
    # entry≈10010, sl≈9510. day1 low=9400 → sl 트리거
    bars = _bars(
        date(2026, 5, 4),
        [
            (10_000, 10_100, 9_900, 10_000),
            (9_950, 10_000, 9_400, 9_500),
        ],
    )
    args = BacktestToolAdapterArgs(
        sheet=sheet,
        bars=[_bar_dict(b) for b in bars],
        capital_krw=1_000_000,
    )

    result = await BacktestToolAdapter().call(args)

    assert result.ok is True
    assert result.value.result.filled is True
    assert result.value.result.exit_reason == "fixed_sl"
    assert result.value.result.pnl_pct < 0
    assert result.metadata["exit_reason"] == "fixed_sl"


# ---------------------------------------------------------------------------
# time_stop trigger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_time_stop_eod_trigger_propagates():
    sheet = _mk_sheet([FixedSlRule(pct=0.10), TimeStopRule(mode="eod")])
    bars = _bars(
        date(2026, 5, 4),
        [
            (10_000, 10_100, 9_900, 10_000),
            (10_000, 10_050, 9_950, 10_020),
        ],
    )
    args = BacktestToolAdapterArgs(
        sheet=sheet,
        bars=[_bar_dict(b) for b in bars],
        capital_krw=1_000_000,
    )

    result = await BacktestToolAdapter().call(args)

    assert result.ok is True
    assert result.value.result.filled is True
    assert result.value.result.exit_reason == "time_stop"
    sells = [t for t in result.value.result.trades if t.side == "sell"]
    assert len(sells) == 1


# ---------------------------------------------------------------------------
# scale_out partial fill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scale_out_partial_fill_records_two_sells():
    sheet = _mk_sheet(
        [
            ScaleOutRule(levels=[(0.03, 0.5)]),
            FixedSlRule(pct=0.10),
            TimeStopRule(mode="hold_days", value=2),
        ]
    )
    bars = _bars(
        date(2026, 5, 4),
        [
            (10_000, 10_100, 9_900, 10_000),
            (10_100, 10_400, 10_000, 10_300),  # +3.9% partial
            (10_300, 10_400, 10_200, 10_300),  # eod
        ],
    )
    args = BacktestToolAdapterArgs(
        sheet=sheet,
        bars=[_bar_dict(b) for b in bars],
        capital_krw=1_000_000,
    )

    result = await BacktestToolAdapter().call(args)

    assert result.ok is True
    sells = [t for t in result.value.result.trades if t.side == "sell"]
    assert len(sells) == 2
    assert sells[0].reason == "scale_out"
    assert sells[1].reason == "time_stop"
    # 수량 보존
    buys = [t for t in result.value.result.trades if t.side == "buy"]
    assert sum(t.qty for t in buys) == sum(t.qty for t in sells)


# ---------------------------------------------------------------------------
# Schema validation failure — raise 하지 않고 ToolResult(ok=False)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_sheet_dict_returns_parse_error_tool_result():
    # exit.rules 에 fixed_sl 빠짐 → PositionSheet validate 실패
    bad_sheet = {
        "sheet_id": "ps_20260504_090000_a1b2",
        "schema_version": "1.1",
        "generated_at": "2026-05-04T09:00:00+09:00",
        "valid_until": "2026-05-04T15:20:00+09:00",
        "ticker": "005930",
        "side": "long",
        "strategy_tag": "GAP_UP_REBOUND",
        "size": {
            "base_pct": 0.05,
            "macro_multiplier": 1.0,
            "risk_multiplier": 1.0,
            "final_pct": 0.05,
            "max_notional_krw": 5_000_000,
        },
        "entry": {
            "trigger": "market",
            "price": None,
            "valid_until": "2026-05-04T15:20:00+09:00",
            "conditions": [],
        },
        # fixed_sl 의도적으로 누락 — schema validator 가 reject 해야 함
        "exit": {
            "rules": [{"type": "time_stop", "mode": "eod"}],
            "priority": "first_match",
        },
        "provenance": {
            "scout_run_id": "scout_test",
            "scout_code_hash": "sha256:test",
            "scout_hypothesis": "test",
            "macro_state_snapshot": {
                "gate": "open",
                "size_multiplier": 1.0,
                "gate_run_id": "macro_test",
            },
            "strategy_policy_version": "test",
            "generated_by": "test",
        },
    }

    bars = [
        {
            "ticker": "005930",
            "price_date": "2026-05-04",
            "open": 10_000,
            "high": 10_100,
            "low": 9_900,
            "close": 10_000,
            "volume": 1_000_000,
        }
    ]

    args = BacktestToolAdapterArgs(sheet=bad_sheet, bars=bars, capital_krw=1_000_000)
    result = await BacktestToolAdapter().call(args)

    assert result.ok is False
    assert result.value is None
    assert result.error_category == ErrorCategory.PARSE_ERROR
    assert result.error is not None and "fixed_sl" in result.error


# ---------------------------------------------------------------------------
# Config override 적용
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_override_changes_entry_price():
    sheet = _mk_sheet([FixedSlRule(pct=0.05), TimeStopRule(mode="hold_days", value=10)])
    bars = _bars(date(2026, 5, 4), [(10_000, 10_100, 9_900, 10_000)] * 5)

    args_no_slip = BacktestToolAdapterArgs(
        sheet=sheet,
        bars=[_bar_dict(b) for b in bars],
        capital_krw=1_000_000,
        config={"slippage_pct": 0.0},
    )
    result = await BacktestToolAdapter().call(args_no_slip)

    assert result.ok is True
    # slippage 0 → entry 가 정확히 open
    assert result.value.result.entry_price == pytest.approx(10_000.0)
