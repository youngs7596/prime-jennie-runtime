"""fast_loop/exit_evaluator 단위 테스트 — 9종 rule + first_match + state 갱신."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from freezegun import freeze_time

from prime_jennie_runtime.fast_loop.domain import PositionState, TickData
from prime_jennie_runtime.fast_loop.exit_evaluator import evaluate
from prime_jennie_runtime.position_sheet.schema import KST, PositionSheet

ENTRY_TS = datetime(2026, 4, 16, 9, 30, 0, tzinfo=KST)
ENTRY_PRICE = 100.0


def _base_sheet_dict(**overrides: Any) -> dict:
    now = datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST)
    data: dict[str, Any] = {
        "sheet_id": "ps_20260416_005930_a3f2",
        "schema_version": "1.1",
        "generated_at": now,
        "valid_until": now + timedelta(hours=6),
        "ticker": "005930",
        "side": "long",
        "strategy_tag": "GAP_UP_REBOUND",
        "size": {
            "base_pct": 0.05,
            "macro_multiplier": 0.7,
            "risk_multiplier": 1.0,
            "final_pct": 0.035,
            "max_notional_krw": 5_000_000,
        },
        "entry": {
            "trigger": "limit",
            "price": 71200,
            "valid_until": now + timedelta(hours=1),
            "conditions": [],
        },
        "exit": {
            "rules": [
                {"type": "fixed_sl", "pct": 0.05},
                {"type": "time_stop", "mode": "eod"},
            ],
            "priority": "first_match",
        },
        "provenance": {
            "scout_run_id": "scout_20260416_0900",
            "scout_code_hash": "sha256:abc123",
            "scout_hypothesis": "테스트 가설",
            "macro_state_snapshot": {
                "gate": "open",
                "size_multiplier": 0.7,
                "gate_run_id": "macro_20260416_0800",
            },
            "news_score_at_generation": 0.3,
            "strategy_policy_version": "v3.0.1",
            "generated_by": "prime-jennie-runtime@v3.0.1",
        },
    }
    data.update(overrides)
    return data


def _sheet_with_rules(rules: list[dict]) -> PositionSheet:
    """`fixed_sl` + `time_stop` 필수 보장 — 부족하면 자동 추가."""
    has_fixed_sl = any(r["type"] == "fixed_sl" for r in rules)
    has_time_stop = any(r["type"] == "time_stop" for r in rules)
    rules_final = list(rules)
    if not has_fixed_sl:
        # pct를 비현실적으로 크게 해서 트리거 안 되게 (테스트가 다른 rule 검증용일 때)
        rules_final.append({"type": "fixed_sl", "pct": 0.09})
    if not has_time_stop:
        rules_final.append({"type": "time_stop", "mode": "eod"})
    return PositionSheet(**_base_sheet_dict(exit={"rules": rules_final, "priority": "first_match"}))


def _fresh_state(entry_price: float = ENTRY_PRICE) -> PositionState:
    return PositionState(
        sheet_id="ps_20260416_005930_a3f2",
        ticker="005930",
        entry_price=entry_price,
        quantity=100,
        entered_at=ENTRY_TS,
        high_watermark=entry_price,
        peak_return_pct=0.0,
    )


def _tick(price: float, *, rsi_1m: float | None = None, ts: datetime | None = None) -> TickData:
    return TickData(
        ticker="005930",
        price=price,
        ts=ts or datetime(2026, 4, 16, 10, 0, 0, tzinfo=KST),
        rsi_1m=rsi_1m,
    )


# ---------------------------------------------------------------------------
# 1) 9종 단일 평가
# ---------------------------------------------------------------------------


class TestFixedSl:
    def test_trigger(self):
        sheet = _sheet_with_rules([{"type": "fixed_sl", "pct": 0.05}])
        state = _fresh_state()
        # -5.1% 손실
        decision = evaluate(sheet, state, _tick(94.9))
        assert decision is not None
        assert decision.should_close is True
        assert decision.reason == "fixed_sl"
        assert decision.portion == 1.0

    def test_no_trigger(self):
        sheet = _sheet_with_rules([{"type": "fixed_sl", "pct": 0.05}])
        state = _fresh_state()
        # -4% 손실 — 미달
        decision = evaluate(sheet, state, _tick(96.0))
        assert decision is None


class TestFixedTp:
    def test_trigger(self):
        sheet = _sheet_with_rules([{"type": "fixed_tp", "pct": 0.05}])
        state = _fresh_state()
        decision = evaluate(sheet, state, _tick(105.0))
        assert decision is not None
        assert decision.should_close is True
        assert decision.reason == "fixed_tp"

    def test_no_trigger(self):
        sheet = _sheet_with_rules([{"type": "fixed_tp", "pct": 0.05}])
        state = _fresh_state()
        decision = evaluate(sheet, state, _tick(104.9))
        assert decision is None


class TestRecoveryExit:
    def test_trigger_on_recovery_above_negative_threshold(self):
        # -1% 회복선: -0.8% 손실로 올라오면 발동
        sheet = _sheet_with_rules([{"type": "recovery_exit", "pct": -0.01}])
        state = _fresh_state()
        decision = evaluate(sheet, state, _tick(99.2))
        assert decision is not None
        assert decision.should_close is True
        assert decision.reason == "recovery_exit"
        assert decision.portion == 1.0

    def test_trigger_when_already_above(self):
        # 양전 상태는 회복선 이상이므로 그대로 발동 (등록 직후 즉시 발동 의도)
        sheet = _sheet_with_rules([{"type": "recovery_exit", "pct": -0.01}])
        state = _fresh_state()
        decision = evaluate(sheet, state, _tick(101.0))
        assert decision is not None
        assert decision.reason == "recovery_exit"

    def test_no_trigger_below_threshold(self):
        # -2% 손실 — 회복선(-1%) 미달
        sheet = _sheet_with_rules([{"type": "recovery_exit", "pct": -0.01}])
        state = _fresh_state()
        assert evaluate(sheet, state, _tick(98.0)) is None

    def test_zero_threshold_means_breakeven_exit(self):
        # pct=0.0: "양전하면 매도" — 본전 미만이면 침묵, 본전부터 발동
        sheet = _sheet_with_rules([{"type": "recovery_exit", "pct": 0.0}])
        state = _fresh_state()
        assert evaluate(sheet, state, _tick(99.9)) is None
        decision = evaluate(sheet, state, _tick(100.0))
        assert decision is not None
        assert decision.reason == "recovery_exit"


class TestTrailingTp:
    def test_trigger_after_activation_and_drop(self):
        sheet = _sheet_with_rules([{"type": "trailing_tp", "activate_pct": 0.05, "drop_pct": 0.03}])
        state = _fresh_state()
        state.trailing_active = True
        state.high_watermark = 110.0
        state.peak_return_pct = 0.10
        # high=110, drop 3% → stop=106.7. price=106.5 미만
        decision = evaluate(sheet, state, _tick(106.5))
        assert decision is not None
        assert decision.should_close is True
        assert decision.reason == "trailing_tp"

    def test_no_trigger_small_drop(self):
        sheet = _sheet_with_rules([{"type": "trailing_tp", "activate_pct": 0.05, "drop_pct": 0.03}])
        state = _fresh_state()
        state.trailing_active = True
        state.high_watermark = 110.0
        # price=107.8 >= 110*0.97=106.7 이므로 미발동
        decision = evaluate(sheet, state, _tick(107.8))
        assert decision is None

    def test_activates_when_reaches_threshold(self):
        sheet = _sheet_with_rules([{"type": "trailing_tp", "activate_pct": 0.05, "drop_pct": 0.03}])
        state = _fresh_state()
        # activate 이전 — +3% 수익만, 미달
        d1 = evaluate(sheet, state, _tick(103.0))
        assert d1 is None
        assert state.trailing_active is False
        # +5% 도달 — 활성화되지만 drop은 안 발생 → None
        d2 = evaluate(sheet, state, _tick(105.0))
        assert d2 is None
        assert state.trailing_active is True


class TestBreakeven:
    def test_activate_and_sl_raise_signal(self):
        sheet = _sheet_with_rules([{"type": "breakeven", "activate_pct": 0.03, "floor_pct": 0.003}])
        state = _fresh_state()
        # +3.5% 도달 → activate
        d1 = evaluate(sheet, state, _tick(103.5))
        assert d1 is not None
        assert d1.should_close is False
        assert d1.reason == "breakeven_sl_raise"
        assert d1.new_sl_price == pytest.approx(100.0 * 1.003)
        assert state.breakeven_active is True
        assert state.breakeven_sl_price == pytest.approx(100.3)

    def test_trigger_close_when_below_floor(self):
        sheet = _sheet_with_rules([{"type": "breakeven", "activate_pct": 0.03, "floor_pct": 0.003}])
        state = _fresh_state()
        state.breakeven_active = True
        state.breakeven_sl_price = 100.3
        # price=100.0 (<= 100.3) → 청산
        decision = evaluate(sheet, state, _tick(100.0))
        assert decision is not None
        assert decision.should_close is True
        assert decision.reason == "breakeven"

    def test_no_trigger_before_activate(self):
        sheet = _sheet_with_rules([{"type": "breakeven", "activate_pct": 0.03, "floor_pct": 0.003}])
        state = _fresh_state()
        # +1% 상승만 (activate 미달), 아직 고점 갱신 안 됨
        decision = evaluate(sheet, state, _tick(101.0))
        assert decision is None
        assert state.breakeven_active is False


class TestScaleOut:
    def test_first_level_triggers(self):
        sheet = _sheet_with_rules([{"type": "scale_out", "levels": [(0.03, 0.25), (0.05, 0.25)]}])
        state = _fresh_state()
        # +3.5% 수익 → level 0 실행
        decision = evaluate(sheet, state, _tick(103.5))
        assert decision is not None
        assert decision.should_close is True
        assert decision.reason == "scale_out"
        assert decision.portion == pytest.approx(0.25)
        assert 0 in state.scale_out_executed

    def test_same_tick_not_reexecuted(self):
        sheet = _sheet_with_rules([{"type": "scale_out", "levels": [(0.03, 0.25), (0.05, 0.25)]}])
        state = _fresh_state()
        state.scale_out_executed.add(0)  # 이미 실행됨
        # +3.5% 수익 — level 0 실행 완료, level 1(+5%) 미달
        decision = evaluate(sheet, state, _tick(103.5))
        assert decision is None

    def test_jump_to_second_level(self):
        sheet = _sheet_with_rules([{"type": "scale_out", "levels": [(0.03, 0.25), (0.05, 0.25)]}])
        state = _fresh_state()
        state.scale_out_executed.add(0)
        # +5% — level 1 실행
        decision = evaluate(sheet, state, _tick(105.0))
        assert decision is not None
        assert decision.portion == pytest.approx(0.25)
        assert 1 in state.scale_out_executed


class TestTimeStop:
    def test_eod_trigger_after_1520(self):
        sheet = _sheet_with_rules([{"type": "time_stop", "mode": "eod"}])
        state = _fresh_state()
        now = datetime(2026, 4, 16, 15, 21, 0, tzinfo=KST)
        decision = evaluate(sheet, state, _tick(100.0), now=now)
        assert decision is not None
        assert decision.should_close is True
        assert decision.reason == "time_stop"

    def test_eod_no_trigger_before_1520(self):
        sheet = _sheet_with_rules([{"type": "time_stop", "mode": "eod"}])
        state = _fresh_state()
        now = datetime(2026, 4, 16, 15, 19, 0, tzinfo=KST)
        decision = evaluate(sheet, state, _tick(100.0), now=now)
        assert decision is None

    def test_hold_days_trigger(self):
        sheet = _sheet_with_rules([{"type": "time_stop", "mode": "hold_days", "value": 2}])
        state = _fresh_state()
        now = datetime(2026, 4, 18, 15, 21, 0, tzinfo=KST)
        decision = evaluate(
            sheet,
            state,
            _tick(100.0),
            now=now,
            entered_business_days=2,
        )
        assert decision is not None
        assert decision.should_close is True
        assert decision.reason == "time_stop"

    def test_hold_days_no_trigger_before_cutoff(self):
        sheet = _sheet_with_rules([{"type": "time_stop", "mode": "hold_days", "value": 2}])
        state = _fresh_state()
        # 영업일 경과했지만 시간 미달
        now = datetime(2026, 4, 18, 14, 0, 0, tzinfo=KST)
        decision = evaluate(
            sheet,
            state,
            _tick(100.0),
            now=now,
            entered_business_days=2,
        )
        assert decision is None

    @freeze_time("2026-04-16 15:21:00+09:00")
    def test_time_stop_with_freezegun(self):
        sheet = _sheet_with_rules([{"type": "time_stop", "mode": "eod"}])
        state = _fresh_state()
        now = datetime.now(tz=KST)
        decision = evaluate(sheet, state, _tick(100.0), now=now)
        assert decision is not None
        assert decision.reason == "time_stop"


class TestOverextension:
    def test_trigger_on_rsi_high(self):
        sheet = _sheet_with_rules([{"type": "overextension_exit", "rsi_threshold": 85}])
        state = _fresh_state()
        decision = evaluate(sheet, state, _tick(101.0, rsi_1m=87.0))
        assert decision is not None
        assert decision.should_close is True
        assert decision.reason == "overextension_exit"
        assert state.overextension_triggered is True

    def test_no_trigger_when_rsi_missing(self):
        sheet = _sheet_with_rules([{"type": "overextension_exit", "rsi_threshold": 85}])
        state = _fresh_state()
        decision = evaluate(sheet, state, _tick(101.0, rsi_1m=None))
        assert decision is None

    def test_no_retrigger_after_fired(self):
        sheet = _sheet_with_rules([{"type": "overextension_exit", "rsi_threshold": 85}])
        state = _fresh_state()
        state.overextension_triggered = True
        decision = evaluate(sheet, state, _tick(101.0, rsi_1m=92.0))
        assert decision is None


class TestProfitFloor:
    def test_trigger_after_activate_then_drop(self):
        sheet = _sheet_with_rules(
            [{"type": "profit_floor", "activate_pct": 0.15, "floor_pct": 0.10}]
        )
        state = _fresh_state()
        state.profit_floor_active = True
        state.high_watermark = 118.0
        state.peak_return_pct = 0.18
        # 현재가 109 → +9%, floor(10%) 미만
        decision = evaluate(sheet, state, _tick(109.0))
        assert decision is not None
        assert decision.should_close is True
        assert decision.reason == "profit_floor"

    def test_no_trigger_before_activate(self):
        sheet = _sheet_with_rules(
            [{"type": "profit_floor", "activate_pct": 0.15, "floor_pct": 0.10}]
        )
        state = _fresh_state()
        # peak=12% only — activate 미달
        state.high_watermark = 112.0
        state.peak_return_pct = 0.12
        decision = evaluate(sheet, state, _tick(108.0))
        assert decision is None
        assert state.profit_floor_active is False


class TestDeathCross:
    @staticmethod
    def _death_cross_closes() -> list[float]:
        """MA5 하향 교차 시나리오. 24일 100에서 마지막 1일 급락 → MA5 < MA20*(1-gap).

        prev MA5 = 100, prev MA20 = 100 (교차 전).
        curr MA5 = (100*4+70)/5 = 94, curr MA20 = (100*19+70)/20 = 98.5 → curr 94 < 98.5*0.998.
        """
        return [100.0] * 24 + [70.0]

    @staticmethod
    def _no_cross_closes() -> list[float]:
        """계속 상승 — death cross 없음."""
        return [100.0 + i * 0.5 for i in range(25)]

    def test_trigger_in_loss_region(self):
        sheet = _sheet_with_rules(
            [
                {
                    "type": "death_cross",
                    "ma_short": 5,
                    "ma_long": 20,
                    "min_loss_pct": 0.01,
                }
            ]
        )
        state = _fresh_state()
        closes = self._death_cross_closes()
        now = datetime(2026, 4, 16, 10, 0, 0, tzinfo=KST)
        # 현재가 98.5 — -1.5% 손실
        decision = evaluate(sheet, state, _tick(98.5, ts=now), now=now, daily_closes=closes)
        assert decision is not None
        assert decision.should_close is True
        assert decision.reason == "death_cross"
        assert state.death_cross_checked_at == now

    def test_no_trigger_in_profit_region(self):
        sheet = _sheet_with_rules(
            [
                {
                    "type": "death_cross",
                    "ma_short": 5,
                    "ma_long": 20,
                    "min_loss_pct": 0.01,
                }
            ]
        )
        state = _fresh_state()
        closes = self._death_cross_closes()
        now = datetime(2026, 4, 16, 10, 0, 0, tzinfo=KST)
        # 수익 +2% 구간 — min_loss_pct 조건 미달
        decision = evaluate(sheet, state, _tick(102.0, ts=now), now=now, daily_closes=closes)
        assert decision is None

    def test_no_trigger_without_daily_closes(self):
        sheet = _sheet_with_rules(
            [
                {
                    "type": "death_cross",
                    "ma_short": 5,
                    "ma_long": 20,
                    "min_loss_pct": 0.01,
                }
            ]
        )
        state = _fresh_state()
        now = datetime(2026, 4, 16, 10, 0, 0, tzinfo=KST)
        decision = evaluate(sheet, state, _tick(98.0, ts=now), now=now, daily_closes=None)
        assert decision is None

    def test_no_retrigger_same_day(self):
        sheet = _sheet_with_rules(
            [
                {
                    "type": "death_cross",
                    "ma_short": 5,
                    "ma_long": 20,
                    "min_loss_pct": 0.01,
                }
            ]
        )
        state = _fresh_state()
        now = datetime(2026, 4, 16, 10, 0, 0, tzinfo=KST)
        state.death_cross_checked_at = now
        closes = self._death_cross_closes()
        decision = evaluate(sheet, state, _tick(98.0, ts=now), now=now, daily_closes=closes)
        assert decision is None


# ---------------------------------------------------------------------------
# 2) first_match 우선순위 (T15~T20)
# ---------------------------------------------------------------------------


class TestFirstMatch:
    def test_profit_floor_before_trailing_tp(self):
        """T17 변형: profit_floor가 trailing_tp보다 위에 있으면 먼저 매칭."""
        sheet = _sheet_with_rules(
            [
                {"type": "profit_floor", "activate_pct": 0.15, "floor_pct": 0.10},
                {"type": "trailing_tp", "activate_pct": 0.05, "drop_pct": 0.03},
            ]
        )
        state = _fresh_state()
        state.profit_floor_active = True
        state.peak_return_pct = 0.18
        state.high_watermark = 118.0
        state.trailing_active = True
        # +9% 수익 — profit_floor floor(10%) 미만 & trailing_tp drop 조건도 만족
        decision = evaluate(sheet, state, _tick(109.0))
        assert decision is not None
        assert decision.reason == "profit_floor"

    def test_activate_miss_falls_through(self):
        """T16: profit_floor activate 미달이면 다음 rule로."""
        sheet = _sheet_with_rules(
            [
                {"type": "profit_floor", "activate_pct": 0.15, "floor_pct": 0.10},
                {"type": "fixed_tp", "pct": 0.02},
            ]
        )
        state = _fresh_state()
        # +2.5% 수익 — profit_floor 미활성, fixed_tp는 매칭
        decision = evaluate(sheet, state, _tick(102.5))
        assert decision is not None
        assert decision.reason == "fixed_tp"

    def test_no_rule_matches(self):
        """T17: 모든 rule 미매칭 → None."""
        sheet = _sheet_with_rules(
            [
                {"type": "fixed_tp", "pct": 0.05},
                {"type": "fixed_sl", "pct": 0.05},
            ]
        )
        state = _fresh_state()
        decision = evaluate(sheet, state, _tick(100.5))
        assert decision is None

    def test_death_cross_profit_region_falls_to_time_stop(self):
        """T18: death_cross 수익 구간 → 미발동, 다른 rule로 fallback."""
        sheet = _sheet_with_rules(
            [
                {
                    "type": "death_cross",
                    "ma_short": 5,
                    "ma_long": 20,
                    "min_loss_pct": 0.01,
                },
                {"type": "fixed_sl", "pct": 0.05},
            ]
        )
        state = _fresh_state()
        closes = TestDeathCross._death_cross_closes()
        now = datetime(2026, 4, 16, 10, 0, 0, tzinfo=KST)
        # 수익 +2% + 교차 발생이지만 min_loss_pct 미충족
        decision = evaluate(sheet, state, _tick(102.0, ts=now), now=now, daily_closes=closes)
        assert decision is None

    def test_death_cross_before_fixed_sl(self):
        """T20: death_cross가 fixed_sl 앞이면 death_cross가 먼저 매칭."""
        sheet = _sheet_with_rules(
            [
                {
                    "type": "death_cross",
                    "ma_short": 5,
                    "ma_long": 20,
                    "min_loss_pct": 0.01,
                },
                {"type": "fixed_sl", "pct": 0.05},
            ]
        )
        state = _fresh_state()
        closes = TestDeathCross._death_cross_closes()
        now = datetime(2026, 4, 16, 10, 0, 0, tzinfo=KST)
        # -3% 손실: death_cross 조건도 충족, fixed_sl 조건은 미충족(-5% 미달)
        decision = evaluate(sheet, state, _tick(97.0, ts=now), now=now, daily_closes=closes)
        assert decision is not None
        assert decision.reason == "death_cross"

    def test_fixed_sl_before_time_stop(self):
        """T19 변형: fixed_sl은 time_stop보다 먼저 평가된다."""
        sheet = _sheet_with_rules(
            [
                {"type": "fixed_sl", "pct": 0.05},
                {"type": "time_stop", "mode": "eod"},
            ]
        )
        state = _fresh_state()
        now = datetime(2026, 4, 16, 15, 21, 0, tzinfo=KST)
        # -6% 손실 & 15:21 동시 — fixed_sl이 배열 앞
        decision = evaluate(sheet, state, _tick(94.0), now=now)
        assert decision is not None
        assert decision.reason == "fixed_sl"


# ---------------------------------------------------------------------------
# 3) state 갱신 검증
# ---------------------------------------------------------------------------


class TestStateUpdates:
    def test_high_watermark_updated_each_tick(self):
        sheet = _sheet_with_rules([{"type": "fixed_tp", "pct": 0.50}])
        state = _fresh_state()
        assert state.high_watermark == 100.0
        evaluate(sheet, state, _tick(105.0))
        assert state.high_watermark == 105.0
        assert state.peak_return_pct == pytest.approx(0.05)
        # 가격 하락 — 고점은 유지
        evaluate(sheet, state, _tick(102.0))
        assert state.high_watermark == 105.0

    def test_scale_out_records_executed_index(self):
        sheet = _sheet_with_rules([{"type": "scale_out", "levels": [(0.03, 0.25), (0.05, 0.25)]}])
        state = _fresh_state()
        assert state.scale_out_executed == set()
        evaluate(sheet, state, _tick(103.5))
        assert state.scale_out_executed == {0}
        # 한 번 더 — 같은 레벨 재실행 금지
        evaluate(sheet, state, _tick(104.0))
        assert state.scale_out_executed == {0}
        # +5% 도달 시 level 1 실행
        evaluate(sheet, state, _tick(105.0))
        assert state.scale_out_executed == {0, 1}

    def test_breakeven_sets_sl_price_on_activate(self):
        sheet = _sheet_with_rules([{"type": "breakeven", "activate_pct": 0.03, "floor_pct": 0.003}])
        state = _fresh_state()
        assert state.breakeven_sl_price is None
        evaluate(sheet, state, _tick(103.5))
        assert state.breakeven_active is True
        assert state.breakeven_sl_price == pytest.approx(100.3)
