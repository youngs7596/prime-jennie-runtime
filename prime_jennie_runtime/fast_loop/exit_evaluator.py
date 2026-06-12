"""10종 Exit Rule 평가 엔진 — first_match 순회.

`sheet.exit.rules`를 배열 순서대로 평가하여 첫 매칭되는 rule의 `ExitDecision`을
반환한다. `breakeven`은 청산이 아닌 SL 상향 보조 rule이므로
`should_close=False` + `new_sl_price`가 채워진 Decision을 내며, `scale_out`은
부분 청산이므로 `portion<1.0`을 돌려주고 이미 실행된 index는 건너뛴다.

상태 객체(`PositionState`)는 평가 내부에서 **직접 변경**된다:
- `update_high(price)`로 고점/peak_return 갱신
- activate 플래그 (`trailing_active`, `profit_floor_active`, `breakeven_active`)
- `scale_out_executed` (실행한 index 저장)
- `death_cross_checked_at` (일봉 기반 rule의 중복 평가 방지)
- `overextension_triggered` (1분봉 RSI 1회만 발동)

v3 schema의 `exit.rules`는 first_match 우선순위이므로 한 tick에서 최대 1개
rule만 실행된다. scale_out 같은 부분 청산 rule도 그 tick에서 한 레벨만.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import TYPE_CHECKING

from prime_jennie_runtime.fast_loop.indicators import check_death_cross
from prime_jennie_runtime.position_sheet.schema import (
    KST,
    BreakevenRule,
    DeathCrossRule,
    FixedSlRule,
    FixedTpRule,
    OverextensionExitRule,
    ProfitFloorRule,
    RecoveryExitRule,
    ScaleOutRule,
    TimeStopRule,
    TrailingTpRule,
)

if TYPE_CHECKING:
    from prime_jennie_runtime.fast_loop.domain import (
        ExitDecision,
        PositionState,
        TickData,
    )
    from prime_jennie_runtime.position_sheet.schema import ExitRule, PositionSheet

# 장 마감 동시호가 직전 청산 기준 시각 (KST)
TIME_STOP_CUTOFF = time(15, 20)


def evaluate(
    sheet: PositionSheet,
    state: PositionState,
    tick: TickData,
    *,
    now: datetime | None = None,
    daily_closes: list[float] | None = None,
    entered_business_days: int = 0,
) -> ExitDecision | None:
    """first_match 평가. 매칭 rule이 없으면 None.

    호출자는 매 tick마다 이 함수를 호출하고 반환된 Decision을 실행한다.
    state 객체는 함수 내에서 직접 수정되므로 호출자가 동일 state를 재사용해야
    plateau/peak 추적이 연속성 있게 동작한다.
    """
    evaluation_ts = now if now is not None else tick.ts

    # 매 tick 고점 갱신 — profit_floor/trailing_tp 모두 의존한다.
    state.update_high(tick.price)
    current_return = state.current_return_pct(tick.price)

    for rule in sheet.exit.rules:
        decision = _evaluate_rule(
            rule,
            state=state,
            tick=tick,
            now=evaluation_ts,
            daily_closes=daily_closes,
            entered_business_days=entered_business_days,
            current_return=current_return,
        )
        if decision is not None:
            return decision

    return None


def _evaluate_rule(
    rule: ExitRule,
    *,
    state: PositionState,
    tick: TickData,
    now: datetime,
    daily_closes: list[float] | None,
    entered_business_days: int,
    current_return: float,
) -> ExitDecision | None:
    """단일 rule 평가 — type에 따라 적절한 체커로 dispatch."""
    if isinstance(rule, FixedSlRule):
        return _check_fixed_sl(rule, state, tick, current_return)
    if isinstance(rule, FixedTpRule):
        return _check_fixed_tp(rule, state, tick, current_return)
    if isinstance(rule, TrailingTpRule):
        return _check_trailing_tp(rule, state, tick, current_return)
    if isinstance(rule, BreakevenRule):
        return _check_breakeven(rule, state, tick, current_return)
    if isinstance(rule, ScaleOutRule):
        return _check_scale_out(rule, state, current_return)
    if isinstance(rule, TimeStopRule):
        return _check_time_stop(rule, now, entered_business_days)
    if isinstance(rule, OverextensionExitRule):
        return _check_overextension(rule, state, tick)
    if isinstance(rule, ProfitFloorRule):
        return _check_profit_floor(rule, state, current_return)
    if isinstance(rule, DeathCrossRule):
        return _check_death_cross(rule, state, tick, now, daily_closes, current_return)
    if isinstance(rule, RecoveryExitRule):
        return _check_recovery_exit(rule, current_return)
    # sheet 스키마가 허용하는 모든 타입은 위에서 커버된다.
    return None


def _make_decision(
    *,
    should_close: bool,
    reason: str,
    rule_type: str,
    portion: float = 1.0,
    new_sl_price: float | None = None,
    metadata: dict | None = None,
) -> ExitDecision:
    from prime_jennie_runtime.fast_loop.domain import ExitDecision

    return ExitDecision(
        should_close=should_close,
        reason=reason,
        portion=portion,
        new_sl_price=new_sl_price,
        matched_rule_type=rule_type,
        metadata=metadata or {},
    )


# ---------------------------------------------------------------------------
# 개별 rule 체커
# ---------------------------------------------------------------------------


def _check_fixed_sl(
    rule: FixedSlRule,
    state: PositionState,
    tick: TickData,
    current_return: float,
) -> ExitDecision | None:
    """진입가 대비 pct 이상 손실이면 전량 청산."""
    # breakeven이 활성화되어 SL이 상향된 경우, fixed_sl은 breakeven 가격이 더
    # 유리하므로 우회시킨다. (breakeven rule이 더 앞 순서라면 이미 처리됨.)
    threshold = state.entry_price * (1.0 - rule.pct)
    if state.breakeven_sl_price is not None and state.breakeven_sl_price > threshold:
        threshold = state.breakeven_sl_price

    if tick.price <= threshold:
        return _make_decision(
            should_close=True,
            reason="fixed_sl",
            rule_type="fixed_sl",
            metadata={"threshold": threshold, "current_return": current_return},
        )
    return None


def _check_fixed_tp(
    rule: FixedTpRule,
    state: PositionState,
    tick: TickData,
    current_return: float,
) -> ExitDecision | None:
    """진입가 대비 pct 이상 수익이면 전량 청산."""
    if current_return >= rule.pct:
        return _make_decision(
            should_close=True,
            reason="fixed_tp",
            rule_type="fixed_tp",
            metadata={"current_return": current_return},
        )
    _ = state, tick  # unused but accepted for dispatch symmetry
    return None


def _check_trailing_tp(
    rule: TrailingTpRule,
    state: PositionState,
    tick: TickData,
    current_return: float,
) -> ExitDecision | None:
    """activate_pct 이상 상승 후 고점 대비 drop_pct 하락 시 청산."""
    if not state.trailing_active:
        if current_return >= rule.activate_pct:
            state.trailing_active = True
        else:
            return None
    # 이미 활성 상태거나 방금 활성화됨 — 고점은 evaluate()에서 갱신됨
    trailing_stop = state.high_watermark * (1.0 - rule.drop_pct)
    if tick.price < trailing_stop:
        return _make_decision(
            should_close=True,
            reason="trailing_tp",
            rule_type="trailing_tp",
            metadata={
                "high_watermark": state.high_watermark,
                "trailing_stop": trailing_stop,
            },
        )
    return None


def _check_breakeven(
    rule: BreakevenRule,
    state: PositionState,
    tick: TickData,
    current_return: float,
) -> ExitDecision | None:
    """activate_pct 도달 1회 후 SL을 진입가*(1+floor_pct)로 상향."""
    if not state.breakeven_active:
        if current_return >= rule.activate_pct:
            state.breakeven_active = True
            state.breakeven_sl_price = state.entry_price * (1.0 + rule.floor_pct)
        else:
            return None

    # 활성화된 상태에서만 floor SL 도달 확인
    assert state.breakeven_sl_price is not None  # 활성 시 반드시 세팅됨
    floor_price = state.breakeven_sl_price

    if tick.price <= floor_price:
        # breakeven 발동으로 인한 청산은 fixed_sl과 구분해서 기록.
        return _make_decision(
            should_close=True,
            reason="breakeven",
            rule_type="breakeven",
            new_sl_price=floor_price,
            metadata={"floor_price": floor_price},
        )

    # 청산은 아니지만 SL 상향 신호만 반환 (should_close=False).
    # 호출자는 new_sl_price를 KIS Gateway에 전달해 SL 주문을 이동시킨다.
    return _make_decision(
        should_close=False,
        reason="breakeven_sl_raise",
        rule_type="breakeven",
        new_sl_price=floor_price,
        metadata={"floor_price": floor_price},
    )


def _check_scale_out(
    rule: ScaleOutRule,
    state: PositionState,
    current_return: float,
) -> ExitDecision | None:
    """levels 순차 실행. 아직 실행하지 않은 최저 index 1개만 발동."""
    for idx, (trigger_pct, portion_pct) in enumerate(rule.levels):
        if idx in state.scale_out_executed:
            continue
        if current_return >= trigger_pct:
            state.scale_out_executed.add(idx)
            return _make_decision(
                should_close=True,
                reason="scale_out",
                rule_type="scale_out",
                portion=portion_pct,
                metadata={
                    "level_index": idx,
                    "trigger_pct": trigger_pct,
                    "current_return": current_return,
                },
            )
        # 첫 미실행 index가 미매칭이면 뒤쪽 level도 매칭될 수 없으므로 즉시 종료.
        return None
    return None


def _check_time_stop(
    rule: TimeStopRule,
    now: datetime,
    entered_business_days: int,
) -> ExitDecision | None:
    """eod: 오늘 15:20 이후. hold_days: value영업일 경과 + 15:20 이후."""
    kst_now = now.astimezone(KST) if now.tzinfo is not None else now.replace(tzinfo=KST)
    cutoff_reached = kst_now.timetz().replace(tzinfo=None) >= TIME_STOP_CUTOFF

    if rule.mode == "eod":
        if cutoff_reached:
            return _make_decision(
                should_close=True,
                reason="time_stop",
                rule_type="time_stop",
                metadata={"mode": "eod", "now": kst_now.isoformat()},
            )
        return None

    # mode == "hold_days"
    if rule.value is None:
        return None
    if entered_business_days >= rule.value and cutoff_reached:
        return _make_decision(
            should_close=True,
            reason="time_stop",
            rule_type="time_stop",
            metadata={
                "mode": "hold_days",
                "value": rule.value,
                "entered_business_days": entered_business_days,
            },
        )
    return None


def _check_overextension(
    rule: OverextensionExitRule,
    state: PositionState,
    tick: TickData,
) -> ExitDecision | None:
    """1분봉 RSI가 threshold 초과 시 청산. 이미 발동했거나 데이터 없으면 건너뜀."""
    if state.overextension_triggered:
        return None
    if tick.rsi_1m is None:
        return None
    if tick.rsi_1m >= rule.rsi_threshold:
        state.overextension_triggered = True
        return _make_decision(
            should_close=True,
            reason="overextension_exit",
            rule_type="overextension_exit",
            metadata={"rsi_1m": tick.rsi_1m, "threshold": rule.rsi_threshold},
        )
    return None


def _check_profit_floor(
    rule: ProfitFloorRule,
    state: PositionState,
    current_return: float,
) -> ExitDecision | None:
    """peak_return_pct이 activate_pct 1회 도달 후 current_return이 floor 미만이면 청산."""
    if not state.profit_floor_active:
        if state.peak_return_pct >= rule.activate_pct:
            state.profit_floor_active = True
        else:
            return None

    if current_return < rule.floor_pct:
        return _make_decision(
            should_close=True,
            reason="profit_floor",
            rule_type="profit_floor",
            metadata={
                "peak_return_pct": state.peak_return_pct,
                "current_return": current_return,
                "floor_pct": rule.floor_pct,
            },
        )
    return None


def _check_recovery_exit(
    rule: RecoveryExitRule,
    current_return: float,
) -> ExitDecision | None:
    """손익률이 임계(0 이하) 이상으로 회복하면 전량 청산 — 사용자 등록 조건부 매도."""
    if current_return >= rule.pct:
        return _make_decision(
            should_close=True,
            reason="recovery_exit",
            rule_type="recovery_exit",
            metadata={"threshold_pct": rule.pct, "current_return": current_return},
        )
    return None


def _check_death_cross(
    rule: DeathCrossRule,
    state: PositionState,
    tick: TickData,
    now: datetime,
    daily_closes: list[float] | None,
    current_return: float,
) -> ExitDecision | None:
    """일봉 MA 하향 교차 + min_loss_pct 이상 손실 구간."""
    if not daily_closes:
        return None

    # 하루 1회만 평가 — 이미 오늘 체크했으면 건너뜀
    if (
        state.death_cross_checked_at is not None
        and state.death_cross_checked_at.date() == now.date()
    ):
        return None

    # 손실 구간 조건: current_return <= -min_loss_pct
    loss_ok = current_return <= -rule.min_loss_pct
    if not loss_ok:
        # 손실 구간이 아니면 오늘은 평가하지 않음 (다른 날 재평가 가능)
        return None

    crossed = check_death_cross(
        daily_closes,
        short=rule.ma_short,
        long=rule.ma_long,
    )
    # 평가 시도 자체를 기록 (수익 구간은 건너뛰었으므로 여기만 표시)
    state.death_cross_checked_at = now

    if crossed:
        return _make_decision(
            should_close=True,
            reason="death_cross",
            rule_type="death_cross",
            metadata={
                "ma_short": rule.ma_short,
                "ma_long": rule.ma_long,
                "current_return": current_return,
            },
        )
    _ = tick  # 현재가는 current_return으로 이미 반영됨
    return None
