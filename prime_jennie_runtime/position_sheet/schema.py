"""포지션 시트 JSON 스키마 v1.1 — POSITION_SHEET_SPEC 전수 구현.

느린 루프(Strategy Engine)가 발행하고, 빠른 루프(Executor)가 소비하는
유일한 인터페이스. 모든 Track이 공유하는 계약 — 수정 시 stop-the-world.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

# KST timezone
KST = timezone(timedelta(hours=9))

# 최소 포지션 비율 — 이 미만은 Strategy Engine이 시트 미발행
MIN_POSITION_PCT = 0.005

# 허용 strategy_tag (추가는 MINOR 업데이트)
ALLOWED_STRATEGY_TAGS = frozenset(
    {
        "GAP_UP_REBOUND",
        "SECTOR_MOMENTUM",
        "EARNINGS_DRIFT",
        "MEAN_REVERT_RSI",
    }
)

# 폐기된 strategy_tag
DEPRECATED_STRATEGY_TAGS = frozenset({"RSI_REBOUND"})

# sheet_id 정규식 — 운영 시트는 `ps_` prefix, 백테스트 영속화 시트는 `bt_` prefix
# (backtest/persistence.py 가 운영 sheet 와 namespace 분리용으로 사용).
SHEET_ID_RE = re.compile(r"^(?:ps|bt)_\d{8}_\d{6}_[0-9a-f]{4}$")


# =====================================================================
# Exit Rules — 10종 discriminated union
# =====================================================================


class FixedSlRule(BaseModel):
    """필수. 진입가 대비 손절."""

    type: Literal["fixed_sl"] = "fixed_sl"
    pct: Annotated[float, Field(gt=0, le=0.10)]


class FixedTpRule(BaseModel):
    """진입가 대비 익절."""

    type: Literal["fixed_tp"] = "fixed_tp"
    pct: Annotated[float, Field(gt=0)]


class TrailingTpRule(BaseModel):
    """활성화 후 고점 대비 하락 시 청산."""

    type: Literal["trailing_tp"] = "trailing_tp"
    activate_pct: Annotated[float, Field(gt=0)]
    drop_pct: Annotated[float, Field(gt=0)]


class BreakevenRule(BaseModel):
    """활성화 후 손익분기 바닥 보호. 청산하지 않고 SL 상향."""

    type: Literal["breakeven"] = "breakeven"
    activate_pct: Annotated[float, Field(gt=0)]
    floor_pct: Annotated[float, Field(gt=0)]


class ScaleOutRule(BaseModel):
    """부분 청산. levels: [[trigger_pct, portion_pct], ...]"""

    type: Literal["scale_out"] = "scale_out"
    levels: list[tuple[float, float]]


class TimeStopRule(BaseModel):
    """시간 기반 청산. eod: 당일 15:20, hold_days: N영업일 후 15:20."""

    type: Literal["time_stop"] = "time_stop"
    mode: Literal["eod", "hold_days"]
    value: int | None = None


class OverextensionExitRule(BaseModel):
    """1분봉 RSI 과열 시 청산."""

    type: Literal["overextension_exit"] = "overextension_exit"
    rsi_threshold: Annotated[float, Field(gt=0, le=100)]


class ProfitFloorRule(BaseModel):
    """고점 수익률 도달 후 바닥 수익률 깨지면 청산."""

    type: Literal["profit_floor"] = "profit_floor"
    activate_pct: Annotated[float, Field(gt=0)]
    floor_pct: Annotated[float, Field(gt=0)]


class DeathCrossRule(BaseModel):
    """이동평균 하향 교차 + 손실 구간 조건."""

    type: Literal["death_cross"] = "death_cross"
    ma_short: int
    ma_long: int
    min_loss_pct: Annotated[float, Field(gt=0)]


class RecoveryExitRule(BaseModel):
    """손익률이 임계 이상으로 회복하면 전량 청산 — 사용자 등록 조건부 매도용.

    pct 는 0 이하 분수 (예: -0.01 = "손실이 -1% 위로 줄어들면 매도", 0.0 = "양전
    하면 매도"). 양수 목표는 fixed_tp 의 역할이라 여기서 받지 않는다. 등록 시점에
    이미 임계 이상이면 다음 tick 에서 즉시 발동한다 (확인 단계에서 발동가를 보여
    주므로 의도된 동작). 2026-06-12 사람-승인 매매 설계 §3-1.
    """

    type: Literal["recovery_exit"] = "recovery_exit"
    pct: Annotated[float, Field(ge=-0.10, le=0.0)]


ExitRule = Annotated[
    FixedSlRule
    | FixedTpRule
    | TrailingTpRule
    | BreakevenRule
    | ScaleOutRule
    | TimeStopRule
    | OverextensionExitRule
    | ProfitFloorRule
    | DeathCrossRule
    | RecoveryExitRule,
    Field(discriminator="type"),
]


# =====================================================================
# Entry Conditions
# =====================================================================


class PriceBelowCondition(BaseModel):
    type: Literal["price_below"] = "price_below"
    value: float


class PriceAboveCondition(BaseModel):
    type: Literal["price_above"] = "price_above"
    value: float


class VolumeOverMa20Condition(BaseModel):
    type: Literal["volume_over_ma20"] = "volume_over_ma20"
    min_ratio: float


class SpreadUnderBpsCondition(BaseModel):
    type: Literal["spread_under_bps"] = "spread_under_bps"
    max_bps: float


class RsiUnderCondition(BaseModel):
    """1분봉 RSI 가 threshold 미만일 때만 진입 (overextension filter).

    design Phase 0 §4.5 의 Overextension Filter 를 entry condition 으로 부착.
    fast_loop ``BarEngine`` 의 ``rsi`` lookup 결과 사용. warm-up 동안 (분봉
    수 부족) RSI 가 None 이면 평가자는 보류 (False) 처리 — 안전 측면.
    """

    type: Literal["rsi_under"] = "rsi_under"
    threshold: Annotated[float, Field(gt=0, le=100)]


class PriceAboveRecentHighCondition(BaseModel):
    """tick.price 가 최근 N 분봉 high 보다 클 때만 진입 (breakout confirmation).

    design Phase 0 §4.5 의 GAP_UP_REBOUND 직전 봉 고가 돌파 진입 시그널.
    fast_loop ``BarEngine.recent_high(lookback)`` 사용. warm-up 동안 (분봉
    수 < lookback) None 이면 보류.
    """

    type: Literal["price_above_recent_high"] = "price_above_recent_high"
    lookback: Annotated[int, Field(ge=1, le=60)] = 5


EntryCondition = Annotated[
    PriceBelowCondition
    | PriceAboveCondition
    | VolumeOverMa20Condition
    | SpreadUnderBpsCondition
    | RsiUnderCondition
    | PriceAboveRecentHighCondition,
    Field(discriminator="type"),
]


# =====================================================================
# Sections
# =====================================================================


class SizeSection(BaseModel):
    base_pct: Annotated[float, Field(gt=0, le=0.10)]
    macro_multiplier: Annotated[float, Field(ge=0, le=1.0)]
    risk_multiplier: Annotated[float, Field(ge=0, le=1.0)]
    final_pct: Annotated[float, Field(ge=0, le=0.10)]
    max_notional_krw: Annotated[int, Field(gt=0)]
    # v2 sizing 동등: 자산비례 cap. None 이면 절대값(max_notional_krw)만 사용.
    max_notional_pct: Annotated[float, Field(gt=0, le=0.25)] | None = None


class EntrySection(BaseModel):
    trigger: Literal["limit", "market"]
    price: float | None = None
    valid_until: datetime
    conditions: list[EntryCondition] = Field(default_factory=list)


class ExitSection(BaseModel):
    rules: list[ExitRule] = Field(min_length=1)
    priority: Literal["first_match"] = "first_match"


class MacroStateSnapshot(BaseModel):
    gate: Literal["open", "closed", "manual_override"]
    size_multiplier: float
    gate_run_id: str


# =====================================================================
# Thesis Spec (G6 thesis-aware exit, 2026-05-17 Phase A 도입)
# =====================================================================
# design: .ai/designs/2026-05-17-g6-thesis-aware-exit.md §4
# Pre-flight: .ai/analyses/2026-05-17-g6-hypothesis-catalog-coverage.md
# 정의 위치 — slow_loop 가 import. position_sheet 가 fundamental layer.


class ThesisCondition(BaseModel):
    """결정론 evaluator 로 평가 가능한 단일 thesis condition.

    catalog 8종 (Pre-flight 2026-05-17 으로 7→8). params 의 type 별 schema
    검증은 Phase B (evaluator) 가 담당, Phase A 는 raw dict 영속만.
    """

    type: Literal[
        "kospi_gate",
        "kospi_change_pct_above",
        "sector_momentum_above",
        "no_risk_event_high",
        "earnings_event_window",
        "rsi_below",
        "price_above_breakout",
        "r20d_above_threshold",
    ]
    params: dict[str, Any] = Field(default_factory=dict)


class ThesisSpec(BaseModel):
    """Scout 가 entry 시점에 생성, ProvenanceSection.thesis_spec 영속.

    natural_language 는 기존 scout_hypothesis 와 동일 — 점진 도입기 호환.
    critical_conditions 는 conditions index 리스트 — LLM 지정 후 policy
    intersection 으로 최종 critical 결정 (design §4.4). Phase A 는 LLM 원본
    그대로 영속, intersection 은 Phase B (evaluator) 가 적용.
    """

    natural_language: str = ""
    conditions: list[ThesisCondition] = Field(default_factory=list)
    critical_conditions: list[int] = Field(default_factory=list)


class ProvenanceSection(BaseModel):
    scout_run_id: str
    scout_code_hash: str
    scout_hypothesis: str
    macro_state_snapshot: MacroStateSnapshot
    # macro_state_snapshot.gate_run_id 와 동일 값이지만 top-level 에 박아 두면
    # position_sheets × macro_runs JOIN 이 `provenance_json->>'macro_run_id'`
    # 단일 인덱스로 끝남. 기존 row 와의 호환을 위해 optional.
    macro_run_id: str | None = None
    news_score_at_generation: float | None = None
    strategy_policy_version: str
    generated_by: str
    # Scout 가 발행한 conviction (0.0~1.0). Phase 0 #1 (conviction × pnl_pct)
    # 분석 + Coordinator advisory policy 에 필요. 기존 row 호환을 위해 optional.
    conviction: float | None = None
    # G6 thesis-aware exit Phase A (2026-05-17). Scout LLM 점진 도입기 동안
    # None 허용 (revaluator 가 None sheet 는 skip — fail-open §5.5 A).
    thesis_spec: ThesisSpec | None = None


# =====================================================================
# PositionSheet — 최상위 모델
# =====================================================================


class PositionSheet(BaseModel):
    """포지션 시트 JSON schema v1.1.

    Strategy Engine이 발행하고 Executor가 소비하는 유일한 인터페이스.
    """

    sheet_id: str
    schema_version: Literal["1.1"] = "1.1"
    generated_at: datetime
    valid_until: datetime
    ticker: str
    side: Literal["long"] = "long"
    strategy_tag: str
    size: SizeSection
    entry: EntrySection
    exit: ExitSection
    provenance: ProvenanceSection

    @model_validator(mode="after")
    def validate_all(self) -> PositionSheet:
        # 1. sheet_id 포맷
        if not SHEET_ID_RE.match(self.sheet_id):
            raise ValueError(f"sheet_id 포맷 불일치: {self.sheet_id}")

        # 2. 시각 일관성
        if self.generated_at >= self.valid_until:
            raise ValueError("generated_at must be before valid_until")
        if self.valid_until - self.generated_at < timedelta(seconds=60):
            raise ValueError("valid_until must be at least 60s after generated_at")
        if self.valid_until.timetz() > time(15, 30, tzinfo=KST):
            raise ValueError("valid_until must be <= 15:30 KST")
        if self.entry.valid_until > self.valid_until:
            raise ValueError("entry.valid_until must be <= sheet.valid_until")

        # 3. size 일관성
        expected = self.size.base_pct * self.size.macro_multiplier * self.size.risk_multiplier
        if abs(self.size.final_pct - expected) >= 1e-9:
            raise ValueError(
                f"size.final_pct({self.size.final_pct}) != base*macro*risk({expected})"
            )
        if self.size.final_pct < MIN_POSITION_PCT:
            raise ValueError(f"final_pct {self.size.final_pct} < MIN {MIN_POSITION_PCT}")

        # 4. entry 일관성
        if self.entry.trigger == "limit" and (self.entry.price is None or self.entry.price <= 0):
            raise ValueError("limit trigger requires positive price")
        if self.entry.trigger == "market" and self.entry.price is not None:
            raise ValueError("market trigger must have price=None")

        # 5. exit 일관성 — fixed_sl, time_stop 필수
        rule_types = {r.type for r in self.exit.rules}
        if "fixed_sl" not in rule_types:
            raise ValueError("exit.rules must include fixed_sl")
        if "time_stop" not in rule_types:
            raise ValueError("exit.rules must include time_stop")

        # scale_out portion 합 체크
        for rule in self.exit.rules:
            if isinstance(rule, ScaleOutRule):
                total_portion = sum(p for _, p in rule.levels)
                if total_portion > 1.0:
                    raise ValueError(f"scale_out portion sum {total_portion} > 1.0")

        # 6. strategy_tag
        if self.strategy_tag in DEPRECATED_STRATEGY_TAGS:
            raise ValueError(f"strategy_tag '{self.strategy_tag}' is deprecated")
        if self.strategy_tag not in ALLOWED_STRATEGY_TAGS:
            raise ValueError(f"strategy_tag '{self.strategy_tag}' not allowed")

        return self
