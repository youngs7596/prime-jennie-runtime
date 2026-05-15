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
    def __init__(
        self,
        active: bool = False,
        recent_stop: bool = False,
        recent_exit_today: bool = False,
    ) -> None:
        self._active = active
        self._recent_stop = recent_stop
        self._recent_exit_today = recent_exit_today
        self.calls: list[str] = []
        self.cooldown_calls: list[str] = []
        self.today_exit_calls: list[str] = []

    async def has_active_sheet_today(self, ticker: str, as_of_date: datetime) -> bool:
        self.calls.append(ticker)
        return self._active

    async def has_recent_stop_loss(self, ticker: str) -> bool:
        self.cooldown_calls.append(ticker)
        return self._recent_stop

    async def has_recent_exit_today(self, ticker: str, as_of_date: datetime) -> bool:
        self.today_exit_calls.append(ticker)
        return self._recent_exit_today


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


@pytest.mark.asyncio
async def test_scout_hint_legacy_trailing_stop_alias_absorbed():
    """LLM 이 옛 형식 trailing_stop 으로 hint 줘도 trailing_tp 로 흡수.

    5-12 사고 회귀 방지: prompts.py v0.3 가 trailing_stop 형식을 가르치며
    수십~수백 candidate 의 sheet 생성을 ValidationError 로 막아 매수 0건 발생.
    """
    from prime_jennie_runtime.slow_loop.scout.schemas import ExitHint

    legacy_rules = [
        {"type": "trailing_stop", "pct": 0.03},
        {"type": "time_stop", "days": 5},  # 옛 형식 days
    ]
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    cand = _candidate(tag="GAP_UP_REBOUND", exit_hint=ExitHint(rules_hint=legacy_rules))
    sheet = await engine.build_sheet(cand, _inputs())
    assert sheet is not None
    rule_types = {r.type for r in sheet.exit.rules}
    # 옛 형식이 정상 변환되어야
    assert "trailing_tp" in rule_types
    assert "trailing_stop" not in rule_types
    # time_stop hold_days 5 로 변환
    time_stop = next(r for r in sheet.exit.rules if r.type == "time_stop")
    assert time_stop.mode == "hold_days"
    assert time_stop.value == 5
    # fixed_sl 누락이었으나 default 에서 보충
    assert "fixed_sl" in rule_types


@pytest.mark.asyncio
async def test_scout_hint_missing_required_rules_filled_from_default():
    """exit_hint 가 fixed_sl/time_stop 둘 다 빠뜨려도 default 에서 채워짐."""
    from prime_jennie_runtime.slow_loop.scout.schemas import ExitHint

    rules = [{"type": "fixed_tp", "pct": 0.08}]  # 익절만
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    cand = _candidate(tag="MEAN_REVERT_RSI", exit_hint=ExitHint(rules_hint=rules))
    sheet = await engine.build_sheet(cand, _inputs())
    assert sheet is not None
    rule_types = {r.type for r in sheet.exit.rules}
    assert "fixed_tp" in rule_types
    assert "fixed_sl" in rule_types
    assert "time_stop" in rule_types


@pytest.mark.asyncio
async def test_scout_hint_unknown_rule_type_dropped():
    """schema 에 없는 type 은 silently 제외 (default 의 안전망은 유지)."""
    from prime_jennie_runtime.slow_loop.scout.schemas import ExitHint

    rules = [
        {"type": "moonshot_exit", "pct": 0.99},  # 가짜 type
        {"type": "fixed_sl", "pct": 0.04},
        {"type": "time_stop", "mode": "hold_days", "value": 3},
    ]
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    cand = _candidate(exit_hint=ExitHint(rules_hint=rules))
    sheet = await engine.build_sheet(cand, _inputs())
    assert sheet is not None
    rule_types = {r.type for r in sheet.exit.rules}
    assert "moonshot_exit" not in rule_types
    assert "fixed_sl" in rule_types
    assert "time_stop" in rule_types


# =====================================================================
# overextension_exit policy (v3.0.4)
# =====================================================================


@pytest.mark.asyncio
async def test_overextension_exit_attached_to_short_term_tags():
    """v3.0.4: GAP_UP_REBOUND / EARNINGS_DRIFT 에 overextension_exit 자동 부착."""
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    for tag in ("GAP_UP_REBOUND", "EARNINGS_DRIFT"):
        sheet = await engine.build_sheet(_candidate(tag=tag), _inputs())
        assert sheet is not None, f"{tag} rejected unexpectedly"
        types = [r.type for r in sheet.exit.rules]
        assert "overextension_exit" in types, f"{tag} missing overextension_exit"
        # first_match 평가에서 가장 앞 (engine 의 _EXIT_RULE_ORDER 정렬)
        assert types[0] == "overextension_exit", f"{tag} overextension_exit not first: {types}"
        # rsi_threshold 80 (보수)
        rule = next(r for r in sheet.exit.rules if r.type == "overextension_exit")
        assert rule.rsi_threshold == 80.0


@pytest.mark.asyncio
async def test_overextension_exit_not_on_other_tags():
    """SECTOR_MOMENTUM / MEAN_REVERT_RSI 는 overextension_exit 미부착 (정책 의도)."""
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    for tag in ("SECTOR_MOMENTUM", "MEAN_REVERT_RSI"):
        sheet = await engine.build_sheet(_candidate(tag=tag), _inputs())
        assert sheet is not None
        types = {r.type for r in sheet.exit.rules}
        assert "overextension_exit" not in types, f"{tag} unexpectedly has overextension_exit"


def test_policy_version_bumped_to_v3_0_4():
    """overextension_exit 도입과 함께 policy_version 증가."""
    policy = load_policy()
    assert policy.version == "v3.0.4"


# =====================================================================
# price_above 자동 부착 (GAP_UP_REBOUND, 2026-05-11)
# =====================================================================


@pytest.mark.asyncio
async def test_gap_up_rebound_auto_price_above_from_hint():
    """GAP_UP_REBOUND 에서 scout 가 price_hint 만 주면 price_above 자동 부착."""
    from prime_jennie_runtime.slow_loop.strategy.engine import PRICE_ABOVE_BREAKOUT_MULT

    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    cand = _candidate(tag="GAP_UP_REBOUND")
    # _candidate 는 price_hint=71200.0 limit trigger.
    sheet = await engine.build_sheet(cand, _inputs())
    assert sheet is not None
    types = [c.type for c in sheet.entry.conditions]
    assert "price_above" in types
    # value 는 price_hint × 1.001 (정적 fallback)
    pa = next(c for c in sheet.entry.conditions if c.type == "price_above")
    assert pa.value == pytest.approx(71200.0 * PRICE_ABOVE_BREAKOUT_MULT, abs=1e-6)


@pytest.mark.asyncio
async def test_gap_up_rebound_respects_scout_explicit_price_above():
    """Scout 가 직접 price_above 를 conditions_hint 로 박았으면 engine 은 덮어쓰지 않음."""
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    cand = ScreeningCandidate(
        ticker="005930",
        strategy_tag="GAP_UP_REBOUND",
        conviction=0.8,
        entry_hint=EntryHint(
            trigger="limit",
            price_hint=71200.0,
            conditions_hint=[{"type": "price_above", "value": 70000.0}],
        ),
    )
    sheet = await engine.build_sheet(cand, _inputs())
    assert sheet is not None
    # scout 가 박은 값 (70000.0) 그대로
    pa_conds = [c for c in sheet.entry.conditions if c.type == "price_above"]
    assert len(pa_conds) == 1
    assert pa_conds[0].value == pytest.approx(70000.0, abs=1e-6)


@pytest.mark.asyncio
async def test_non_gap_up_rebound_no_auto_price_above():
    """SECTOR_MOMENTUM 등 다른 전략에선 자동 부착 안 함."""
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    for tag in ("SECTOR_MOMENTUM", "EARNINGS_DRIFT", "MEAN_REVERT_RSI"):
        sheet = await engine.build_sheet(_candidate(tag=tag), _inputs())
        assert sheet is not None
        types = {c.type for c in sheet.entry.conditions}
        assert "price_above" not in types, f"{tag} unexpected price_above"


@pytest.mark.asyncio
async def test_gap_up_rebound_market_trigger_no_price_hint_no_auto():
    """price_hint 없으면 (market trigger 등) 자동 부착 skip — 기준값 없음."""
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    cand = ScreeningCandidate(
        ticker="005930",
        strategy_tag="GAP_UP_REBOUND",
        conviction=0.8,
        entry_hint=EntryHint(trigger="market"),  # price_hint=None
    )
    sheet = await engine.build_sheet(cand, _inputs())
    assert sheet is not None
    types = {c.type for c in sheet.entry.conditions}
    assert "price_above" not in types


# =====================================================================
# protocol 형태 체크
# =====================================================================


def test_active_checker_is_protocol():
    """ActiveSheetChecker는 Protocol — duck typing."""
    assert isinstance(_StubChecker(), ActiveSheetChecker)


# =====================================================================
# macro_run_id provenance (migration 012)
# =====================================================================


@pytest.mark.asyncio
async def test_provenance_exposes_macro_run_id_at_top_level():
    """provenance.macro_run_id == macro_state_snapshot.gate_run_id — SQL 조인 단순화."""
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    sheet = await engine.build_sheet(_candidate(), _inputs())
    assert sheet is not None
    assert sheet.provenance.macro_run_id == "macro_20260416_0800"
    assert sheet.provenance.macro_state_snapshot.gate_run_id == "macro_20260416_0800"


# =====================================================================
# build_sheet_with_reason — 거부 사유 코드화 (migration 012)
# =====================================================================


@pytest.mark.asyncio
async def test_build_sheet_with_reason_success_returns_none_reason():
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    sheet, reason = await engine.build_sheet_with_reason(_candidate(), _inputs())
    assert sheet is not None
    assert reason is None


@pytest.mark.asyncio
async def test_build_sheet_with_reason_macro_closed():
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    sheet, reason = await engine.build_sheet_with_reason(
        _candidate(), _inputs(gate="closed", size_mult=0.0)
    )
    assert sheet is None
    assert reason == "macro_closed"


@pytest.mark.asyncio
async def test_build_sheet_with_reason_deprecated_tag():
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    sheet, reason = await engine.build_sheet_with_reason(_candidate(tag="RSI_REBOUND"), _inputs())
    assert sheet is None
    assert reason == "deprecated_tag"


@pytest.mark.asyncio
async def test_build_sheet_with_reason_unknown_tag():
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    sheet, reason = await engine.build_sheet_with_reason(_candidate(tag="CUSTOM_TAG"), _inputs())
    assert sheet is None
    assert reason == "unknown_tag"


@pytest.mark.asyncio
async def test_build_sheet_with_reason_size_below_min():
    engine = StrategyEngine(load_policy(), FixedRiskThrottle(0.25))
    sheet, reason = await engine.build_sheet_with_reason(_candidate(), _inputs(size_mult=0.25))
    assert sheet is None
    assert reason == "size_below_min"


@pytest.mark.asyncio
async def test_build_sheet_with_reason_duplicate_today():
    checker = _StubChecker(active=True)
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle(), active_checker=checker)
    sheet, reason = await engine.build_sheet_with_reason(_candidate(), _inputs())
    assert sheet is None
    assert reason == "duplicate_today"


@pytest.mark.asyncio
async def test_build_sheet_with_reason_recent_stoploss_cooldown():
    """audit C — 24h 내 손절 이력 있는 ticker → sheet 발행 차단."""
    checker = _StubChecker(active=False, recent_stop=True)
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle(), active_checker=checker)
    sheet, reason = await engine.build_sheet_with_reason(_candidate(), _inputs())
    assert sheet is None
    assert reason == "recent_stoploss_cooldown"
    # duplicate 검사 통과 후 cooldown 검사 호출
    assert checker.calls == ["005930"]
    assert checker.cooldown_calls == ["005930"]


@pytest.mark.asyncio
async def test_cooldown_not_called_when_duplicate_today():
    """duplicate 가드가 먼저 발화하면 cooldown 검사 skip (cost saving)."""
    checker = _StubChecker(active=True, recent_stop=True)
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle(), active_checker=checker)
    sheet, reason = await engine.build_sheet_with_reason(_candidate(), _inputs())
    assert sheet is None
    assert reason == "duplicate_today"
    assert checker.cooldown_calls == []  # duplicate 분기에서 early return


@pytest.mark.asyncio
async def test_build_sheet_with_reason_today_exit_cooldown():
    """today_exit_cooldown — 익절 포함 같은 거래일 exit 이력 시 sheet 차단.

    근거 (5-15 011070): trailing_tp +3.66% 후 13분 만에 같은 종목 재진입 →
    fixed_sl -4.11%. recent_stop_loss 가드 (3b) 는 익절 제외라 못 막음.
    """
    checker = _StubChecker(active=False, recent_stop=False, recent_exit_today=True)
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle(), active_checker=checker)
    sheet, reason = await engine.build_sheet_with_reason(_candidate(), _inputs())
    assert sheet is None
    assert reason == "today_exit_cooldown"
    # duplicate / cooldown 통과 후 today_exit 가드 호출
    assert checker.today_exit_calls == ["005930"]


@pytest.mark.asyncio
async def test_today_exit_not_called_when_recent_stoploss():
    """recent_stoploss 가 먼저 발화하면 today_exit 검사 skip."""
    checker = _StubChecker(active=False, recent_stop=True, recent_exit_today=True)
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle(), active_checker=checker)
    sheet, reason = await engine.build_sheet_with_reason(_candidate(), _inputs())
    assert sheet is None
    assert reason == "recent_stoploss_cooldown"
    assert checker.today_exit_calls == []


# =====================================================================
# entry_hint 정규화 (5-13 MEAN_REVERT_RSI engine_error 회귀 차단)
# =====================================================================


@pytest.mark.asyncio
async def test_scout_hint_limit_with_null_price_normalized_to_market():
    """limit trigger + price_hint=None → market 으로 정규화 후 sheet 발행 성공.

    5-13 MEAN_REVERT_RSI 의 2건 (000250, 352820) engine_error 시나리오 회귀 차단.
    PositionSheet schema 가 limit+null 을 거절하던 케이스.
    """
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    cand = ScreeningCandidate(
        ticker="000250",
        strategy_tag="MEAN_REVERT_RSI",
        conviction=0.5,
        entry_hint=EntryHint(trigger="limit", price_hint=None),
        exit_hint=None,
        factors={"mean_revert_score": 0.5},
        notes="",
    )
    sheet = await engine.build_sheet(cand, _inputs())
    assert sheet is not None
    assert sheet.entry.trigger == "market"
    assert sheet.entry.price is None


@pytest.mark.asyncio
async def test_scout_hint_limit_with_zero_price_normalized_to_market():
    """limit + price_hint=0 (또는 음수) 도 동일하게 market 으로 fallback."""
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    cand = ScreeningCandidate(
        ticker="352820",
        strategy_tag="MEAN_REVERT_RSI",
        conviction=0.5,
        entry_hint=EntryHint(trigger="limit", price_hint=0.0),
        exit_hint=None,
        factors={"mean_revert_score": 0.3},
        notes="",
    )
    sheet = await engine.build_sheet(cand, _inputs())
    assert sheet is not None
    assert sheet.entry.trigger == "market"


@pytest.mark.asyncio
async def test_scout_hint_limit_with_positive_price_unchanged():
    """limit + 양수 price_hint 는 정규화 영향 없음 (정상 케이스 회귀 보호)."""
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    sheet = await engine.build_sheet(_candidate(), _inputs())
    assert sheet is not None
    assert sheet.entry.trigger == "limit"
    assert sheet.entry.price == 71200.0


@pytest.mark.asyncio
async def test_scout_hint_market_with_null_price_unchanged():
    """market + price_hint=None — 정상, 정규화 영향 없음."""
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    cand = ScreeningCandidate(
        ticker="005930",
        strategy_tag="SECTOR_MOMENTUM",
        conviction=0.7,
        entry_hint=EntryHint(trigger="market", price_hint=None),
        exit_hint=None,
        factors={"momentum_5d": 0.034},
        notes="",
    )
    sheet = await engine.build_sheet(cand, _inputs())
    assert sheet is not None
    assert sheet.entry.trigger == "market"


@pytest.mark.asyncio
async def test_gap_up_rebound_limit_with_null_price_no_auto_price_above():
    """GAP_UP_REBOUND + limit + null price → market 으로 정규화되어 price_above
    자동 부착이 적용되지 않음 (price 기준이 사라졌으므로). 의도 손실은 prompt
    가이드 (v0.5) 로 미연에 차단.
    """
    engine = StrategyEngine(load_policy(), NoOpRiskThrottle())
    cand = ScreeningCandidate(
        ticker="000250",
        strategy_tag="GAP_UP_REBOUND",
        conviction=0.5,
        entry_hint=EntryHint(trigger="limit", price_hint=None),
        exit_hint=None,
        factors={"momentum_5d": 0.034},
        notes="",
    )
    sheet = await engine.build_sheet(cand, _inputs())
    assert sheet is not None
    assert sheet.entry.trigger == "market"
    # price 기준 없으니 price_above 자동 부착 안 됨
    assert not any(c.type == "price_above" for c in sheet.entry.conditions)
