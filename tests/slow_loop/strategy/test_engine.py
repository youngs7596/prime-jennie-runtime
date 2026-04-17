"""StrategyEngine.build_sheet 테스트.

검증 시나리오:
- Macro closed → None
- final_pct < MIN_POSITION_PCT → None
- 정상 시 PositionSheet 유효
- Scout exit_hint 없을 때 policy 기본값 사용 + 권장 순서 정렬
- 중복 ticker (같은 날 활성 시트) → None
- 폐기 tag(RSI_REBOUND) → None
- Unknown tag → None
- fixed_sl/time_stop 자동 포함 확인
"""

from __future__ import annotations

from datetime import datetime

import pytest

from prime_jennie_runtime.position_sheet.schema import KST, MacroStateSnapshot
from prime_jennie_runtime.slow_loop.scout.schemas import EntryHint, ScreeningCandidate
from prime_jennie_runtime.slow_loop.strategy.engine import (
    ActiveSheetChecker,
    StrategyEngine,
    StrategyEngineInputs,
)
from prime_jennie_runtime.slow_loop.strategy.policy import load_policy
from prime_jennie_runtime.slow_loop.strategy.risk_throttle import (
    FixedRiskThrottle,
    NoOpRiskThrottle,
)

# =====================================================================
# helpers
# =====================================================================


class _StubChecker:
    def __init__(self, active: bool = False) -> None:
        self._active = active
        self.calls: list[str] = []

    async def has_active_sheet_today(self, ticker: str, as_of_date: datetime) -> bool:
        self.calls.append(ticker)
        return self._active


def _candidate(
    ticker: str = "005930",
    tag: str = "SECTOR_MOMENTUM",
    conviction: float = 0.7,
    exit_hint=None,
) -> ScreeningCandidate:
    return ScreeningCandidate(
        ticker=ticker,
        strategy_tag=tag,
        conviction=conviction,
        entry_hint=EntryHint(trigger="limit", price_hint=71200.0),
        exit_hint=exit_hint,
        factors={"momentum_5d": 0.034},
        notes="테스트",
    )


def _inputs(
    gate: str = "open",
    size_mult: float = 1.0,
    generated_at: datetime | None = None,
) -> StrategyEngineInputs:
    return StrategyEngineInputs(
        macro_state=MacroStateSnapshot(
            gate=gate,  # type: ignore[arg-type]
            size_multiplier=size_mult,
            gate_run_id="macro_20260416_0800",
        ),
        scout_run_id="scout_20260416_0900",
        scout_code_hash="sha256:test",
        scout_hypothesis="테스트 가설",
        generated_at=generated_at or datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST),
        news_score=0.3,
    )


# =====================================================================
# 기본 동작
# =====================================================================


@pytest.mark.asyncio
async def test_normal_open_publishes_sheet():
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    sheet = await engine.build_sheet(_candidate(), _inputs())
    assert sheet is not None
    assert sheet.ticker == "005930"
    assert sheet.strategy_tag == "SECTOR_MOMENTUM"
    # SECTOR_MOMENTUM base_pct=0.05, macro=1.0, risk=1.0
    assert sheet.size.final_pct == pytest.approx(0.05, abs=1e-9)


@pytest.mark.asyncio
async def test_macro_closed_returns_none():
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    sheet = await engine.build_sheet(_candidate(), _inputs(gate="closed", size_mult=0.0))
    assert sheet is None


@pytest.mark.asyncio
async def test_below_min_pct_returns_none():
    """base_pct=0.05 × macro=0.25 × risk=0.25 = 0.003125 < 0.005."""
    engine = StrategyEngine(load_policy(), FixedRiskThrottle(0.25))
    sheet = await engine.build_sheet(_candidate(), _inputs(size_mult=0.25))
    assert sheet is None


@pytest.mark.asyncio
async def test_exactly_min_pct_publishes():
    """base=0.05 × macro=0.5 × risk=0.25 = 0.00625 >= 0.005 OK."""
    engine = StrategyEngine(load_policy(), FixedRiskThrottle(0.25))
    sheet = await engine.build_sheet(_candidate(), _inputs(size_mult=0.5))
    assert sheet is not None
    assert sheet.size.final_pct == pytest.approx(0.00625, abs=1e-9)


@pytest.mark.asyncio
async def test_deprecated_tag_returns_none():
    """RSI_REBOUND (폐기) → None (T07)."""
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    sheet = await engine.build_sheet(_candidate(tag="RSI_REBOUND"), _inputs())
    assert sheet is None


@pytest.mark.asyncio
async def test_unknown_tag_returns_none():
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    sheet = await engine.build_sheet(_candidate(tag="CUSTOM_TAG"), _inputs())
    assert sheet is None


@pytest.mark.asyncio
async def test_duplicate_sheet_rejected():
    """T10: 같은 ticker 오늘 활성 시트 존재 → None."""
    checker = _StubChecker(active=True)
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle(), active_checker=checker)
    sheet = await engine.build_sheet(_candidate(), _inputs())
    assert sheet is None
    assert checker.calls == ["005930"]


@pytest.mark.asyncio
async def test_duplicate_checker_not_called_when_macro_closed():
    """Macro closed면 일찍 반환, checker 호출 안 함."""
    checker = _StubChecker(active=True)
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle(), active_checker=checker)
    sheet = await engine.build_sheet(_candidate(), _inputs(gate="closed", size_mult=0.0))
    assert sheet is None
    assert checker.calls == []


# =====================================================================
# exit rules 권장 순서 (POSITION_SHEET_SPEC §5.3)
# =====================================================================


@pytest.mark.asyncio
async def test_exit_rules_in_recommended_order():
    """SECTOR_MOMENTUM 기본 rules 권장 순서 — profit_floor, trailing_tp, death_cross, fixed_sl, time_stop."""  # noqa: E501
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    sheet = await engine.build_sheet(_candidate(), _inputs())
    assert sheet is not None
    rule_types = [r.type for r in sheet.exit.rules]
    # profit_floor가 trailing_tp 앞, death_cross가 fixed_sl 앞, time_stop이 마지막
    assert rule_types.index("profit_floor") < rule_types.index("trailing_tp")
    assert rule_types.index("death_cross") < rule_types.index("fixed_sl")
    assert rule_types.index("fixed_sl") < rule_types.index("time_stop")
    assert rule_types[-1] == "time_stop"


@pytest.mark.asyncio
async def test_exit_rules_scout_hint_overrides_defaults():
    """Scout가 exit_hint 주면 기본값 대신 그것 사용 (단 fixed_sl/time_stop 포함해야 함)."""
    from prime_jennie_runtime.slow_loop.scout.schemas import ExitHint

    custom_rules = [
        {"type": "fixed_sl", "pct": 0.025},
        {"type": "time_stop", "mode": "hold_days", "value": 2},
        {"type": "fixed_tp", "pct": 0.05},
    ]
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    cand = _candidate(exit_hint=ExitHint(rules_hint=custom_rules))
    sheet = await engine.build_sheet(cand, _inputs())
    assert sheet is not None
    rule_types = [r.type for r in sheet.exit.rules]
    assert "fixed_tp" in rule_types
    # fixed_tp가 fixed_sl 앞 (권장 순서)
    assert rule_types.index("fixed_tp") < rule_types.index("fixed_sl")


@pytest.mark.asyncio
async def test_all_sheets_have_fixed_sl_and_time_stop():
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    for tag in ("GAP_UP_REBOUND", "SECTOR_MOMENTUM", "EARNINGS_DRIFT", "MEAN_REVERT_RSI"):
        sheet = await engine.build_sheet(_candidate(tag=tag), _inputs())
        assert sheet is not None, f"{tag} rejected unexpectedly"
        rule_types = {r.type for r in sheet.exit.rules}
        assert "fixed_sl" in rule_types
        assert "time_stop" in rule_types


# =====================================================================
# protocol 형태 체크
# =====================================================================


def test_active_checker_is_protocol():
    """ActiveSheetChecker는 Protocol — duck typing."""
    assert isinstance(_StubChecker(), ActiveSheetChecker)
