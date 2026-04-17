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


class NewsScoreEntry(BaseModel):
    """ticker별 뉴스 감성 스냅샷 (SCOUT §2.4)."""

    score: Annotated[float, Field(ge=-1.0, le=1.0)]
    timestamp: datetime
    article_count: Annotated[int, Field(ge=0)]
    staleness_hours: Annotated[float, Field(ge=0.0)]


class MacroStateForScout(BaseModel):
    """Scout 프롬프트에 주입할 최소 Macro 상태 (로깅 전용 reasoning 등 제외)."""

    gate: Literal["open", "closed"]
    size_multiplier: Annotated[float, Field(ge=0.0, le=1.0)]
    gate_run_id: str
    top_risks_summary: str = ""


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


# =====================================================================
# Scout Context (LLM 호출 전 조립)
# =====================================================================


class ScoutContext(BaseModel):
    """Scout Agent 호출 시점에 필요한 전체 입력 (SCOUT §2.1)."""

    as_of: date
    universe: list[str]
    market_summary: MarketSummary
    macro_state: MacroStateForScout
    news_scores: dict[str, NewsScoreEntry]
    sector_momentum: dict[str, float]
    previous_scout_runs: list[ScoutRunSummary] = Field(default_factory=list)
    strategy_tags_available: list[str]
    trigger_reason: str = "scheduled_0830"


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
