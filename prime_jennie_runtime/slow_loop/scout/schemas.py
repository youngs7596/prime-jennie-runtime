"""Scout 입출력 Pydantic 스키마.

SCOUT_CODE_GENERATION §2~§3 구현.
- ScoutOutput: LLM structured output
- ScreeningCandidate: Scout 코드가 반환하는 단일 후보
- ScoutContext: Scout 프롬프트 구성에 쓰는 입력 스냅샷
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

# =====================================================================
# 뉴스 / 매크로 입력 보조 타입
# =====================================================================


POSITIVE_EVENT_TYPES = frozenset(
    {"earnings", "contract", "product", "investment", "shareholder_return"}
)
RISK_EVENT_TYPES = frozenset({"geopolitical", "regulation", "strike", "lawsuit", "bankruptcy"})


class NewsEventEntry(BaseModel):
    """ticker별 뉴스 이벤트 분포 스냅샷 (SCOUT §2.4, 2026-04-25 재설계).

    Qwen3 메타데이터 기반. "점수 평균" 을 쓰지 않고 임팩트 × 이벤트 종류별 건수를
    그대로 넘겨서 Scout 코드가 규칙 조합으로 판단. ``positive_events`` /
    ``risk_events`` 는 ``events_by_impact['high']`` 중 긍정·리스크 카테고리 이벤트
    종류를 미리 추출해둔 리스트 — scout code 가 간단히 boolean 체크하도록.
    """

    article_count: Annotated[int, Field(ge=0)]
    latest_at: datetime | None = None  # 기사 0건이면 None
    staleness_hours: Annotated[float, Field(ge=0.0)]
    events_by_impact: dict[str, dict[str, int]] = Field(default_factory=dict)
    # events_by_impact["high"|"medium"|"low"] = {event_type: count}
    positive_events: list[str] = Field(default_factory=list)
    risk_events: list[str] = Field(default_factory=list)


class MacroStateForScout(BaseModel):
    """Scout 프롬프트에 주입할 최소 Macro 상태 (로깅 전용 reasoning 등 제외)."""

    gate: Literal["open", "closed"]
    size_multiplier: Annotated[float, Field(ge=0.0, le=1.0)]
    gate_run_id: str
    top_risks_summary: str = ""


class ConsensusEntry(BaseModel):
    """ticker별 Forward 컨센서스 데이터 (Phase 2 신설).

    v2 의 ``ConsensusInfo`` 와 1:1 대응. v3 DB 에 아직 적재 파이프라인이 없어
    실 feeder 는 후속 작업 — 인터페이스만 먼저 잡고 ``EmptyConsensusFeeder`` 가
    모든 키를 None 으로 반환한다. None 인 필드는 Scout 코드가 missing 으로 취급.

    필드 의미 (v2 enrichment.py 컨벤션):
    - ``forward_per``: 12개월 forward PER
    - ``forward_eps``: 12개월 forward EPS
    - ``analyst_count``: 컨센서스에 포함된 애널리스트 수 (신뢰도 가늠)
    - ``eps_revision_pct``: 최근 30일 forward EPS 변화율 (%, 상향 조정 시 양수)
    - ``target_price``: 목표가 평균
    - ``investment_opinion``: 의견 점수 평균 (1=Strong Buy ~ 5=Sell, broker 컨벤션 따름)
    """

    forward_per: float | None = None
    forward_eps: float | None = None
    analyst_count: int | None = None
    eps_revision_pct: float | None = None
    target_price: float | None = None
    investment_opinion: float | None = None


class MarketSummary(BaseModel):
    """프롬프트에 넣는 거시 요약 (DataFrame 대신 숫자만)."""

    kospi_close: float
    kospi_change_pct: float
    kosdaq_close: float
    kosdaq_change_pct: float
    up_count: int
    down_count: int


class ScoutRunSummary(BaseModel):
    """이전 Scout run 압축 요약 (프롬프트 주입용)."""

    scout_run_id: str
    generated_at: datetime
    hypothesis: str
    candidate_count: int
    hit_rate: float | None = None  # 아직 체결 결과 없으면 None


class PreviousOutcome(BaseModel):
    """Scout 직전 추천의 실현 결과 (G1 outcome 피드백, 2026-05-15 도입).

    `scout_outcomes_v1` view 에서 최근 7일 ticker-strategy 단위 추출. exit_at
    이 있는 row 만 노출 (체결 후 청산 완료). prompt 에서 손절율 / 평균 PnL
    / 최근 손절 ticker 목록을 LLM 에 노출 — informational, enforcement 는
    후행 layer (G2 overextension validator 등) 가 담당.

    design: .ai/designs/2026-05-15-scout-overextension-guards.md §3 G1
    """

    ticker: str
    strategy_tag: str
    generated_at: datetime
    exit_at: datetime
    exit_reason: str | None
    pnl_pct: float | None  # decimal (예: -0.0517 = -5.17%)


class CodeAttemptHint(BaseModel):
    """동일 scout 호출 내 직전 시도의 실행 결과 — 재시도 시 LLM 에 주입.

    sandbox 실행에서 발생한 에러 (NameError, invalid_return_type 등) 를 그대로
    LLM 에게 보여줘서 같은 실수를 반복하지 않게 한다. SCOUT_CODE_GENERATION
    재설계 (2026-04-25 검증 루프) 에서 신설.
    """

    attempt_no: Annotated[int, Field(ge=1)]
    error: str  # ScreeningResult.error (예: "runtime_error", "invalid_return_type")
    details: str  # ScreeningResult.details — traceback 또는 상세 메시지
    code_excerpt: str  # 첫 ~30 줄 발췌 (전체는 길어서 truncate)


# =====================================================================
# Scout Context (LLM 호출 전 조립)
# =====================================================================


class ScoutContext(BaseModel):
    """Scout Agent 호출 시점에 필요한 전체 입력 (SCOUT §2.1)."""

    as_of: date
    universe: list[str]
    market_summary: MarketSummary
    macro_state: MacroStateForScout
    news_events: dict[str, NewsEventEntry]
    sector_momentum: dict[str, float]
    previous_scout_runs: list[ScoutRunSummary] = Field(default_factory=list)
    strategy_tags_available: list[str]
    trigger_reason: str = "scheduled_0830"
    # 같은 호출 내 재시도 시 직전 시도(들)의 실패 정보. 첫 시도엔 빈 리스트.
    previous_attempts: list[CodeAttemptHint] = Field(default_factory=list)
    # Forward 컨센서스 (v3 DB 적재 전 — 현재 모두 None). 후속 작업에서 RealConsensusFeeder
    # 가 채운다. ``{}`` 면 feeder 미주입, 값이 있으면 ticker 별 ConsensusEntry.
    consensus_data: dict[str, ConsensusEntry] = Field(default_factory=dict)
    # Audit B1 (Scout history blindness) 대응 — 2026-05-15 도입.
    # 같은 거래일 이미 sheet 발행된 ticker. LLM 이 중복 추천 자체검열 가능.
    today_entries: list[str] = Field(default_factory=list)
    # 24h 내 손절 (fixed_sl/stop_loss/breakeven_stop) 발생한 ticker. cooldown 가드
    # 와 동일 데이터셋 — Scout 단계에서 자체 회피 + fast_loop enforcement 가 2nd layer.
    recent_stop_loss_tickers: list[str] = Field(default_factory=list)
    # 같은 KST 거래일 내 청산 (exit_reason 무관) 발생한 ticker — 2026-05-15 도입.
    # 데이터 근거: 5월 첫 주 "손절→재진입→손절" 11건 + "익절→재진입→손절" 2건 vs
    # "익절→재진입→회복" 2건. Strategy Engine sheet 발행 단계에서 차단.
    recently_exited_today_tickers: list[str] = Field(default_factory=list)
    # G1 outcome 피드백 (2026-05-15 도입) — 최근 7일 청산 완료 추천 결과.
    # scout_outcomes_v1 view 에서 추출. enforcement 없음 (informational only).
    previous_outcomes: list[PreviousOutcome] = Field(default_factory=list)


# =====================================================================
# Entry / Exit hint (Scout 코드가 Strategy Engine에 건네는 힌트)
# =====================================================================


class EntryHint(BaseModel):
    """Strategy Engine이 시트 entry 섹션 조립에 참고."""

    trigger: Literal["limit", "market"]
    price_hint: float | None = None
    conditions_hint: list[dict[str, Any]] = Field(default_factory=list)


class ExitHint(BaseModel):
    """Scout가 전략별 exit rule을 override할 때 사용."""

    rules_hint: list[dict[str, Any]]


# =====================================================================
# Screening Candidate (Scout 코드 반환 타입)
# =====================================================================


class ScreeningCandidate(BaseModel):
    """Scout 생성 코드가 반환하는 단일 후보 (SCOUT §3.2)."""

    ticker: str
    strategy_tag: str
    conviction: Annotated[float, Field(ge=0.0, le=1.0)]
    entry_hint: EntryHint
    exit_hint: ExitHint | None = None
    factors: dict[str, float] = Field(default_factory=dict)
    notes: str = ""


# =====================================================================
# Scout 출력 (LLM structured output)
# =====================================================================


class ScoutOutput(BaseModel):
    """Scout LLM의 structured output (SCOUT §3.1)."""

    screening_code: str
    hypothesis: Annotated[str, Field(max_length=200)]
    expected_candidates: Annotated[int, Field(ge=0, le=20)]
    factor_weights: dict[str, float]
    strategy_tags_used: list[str]
    fallback_strategy: Literal["skip_today", "use_previous_run", "relax_filters"] = "skip_today"
    estimated_runtime_seconds: Annotated[float, Field(ge=0.0, le=300.0)] = 10.0
