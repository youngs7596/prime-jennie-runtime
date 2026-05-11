"""Macro Gate 입출력 Pydantic 스키마.

MACRO_GATE_SPEC §2~§3 구현.
- MacroGateOutput: LLM structured output (gate, size_multiplier, 로깅용 필드)
- MarketSnapshot: 지수/환율/변동성 스냅샷 (결정론 closed 조건 판정에도 사용)
- MacroContext: Macro Gate 프롬프트 구성에 쓰는 입력
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field, field_validator

# =====================================================================
# 보조 타입
# =====================================================================


_RISK_CATEGORIES = frozenset(
    {"geopolitical", "monetary", "liquidity", "sector_contagion", "fx", "commodity", "other"}
)


class RiskItem(BaseModel):
    """top_risks 항목 (MACRO §2.1). severity critical이 1개 이상이면 강한 반영."""

    category: Literal[
        "geopolitical",
        "monetary",
        "liquidity",
        "sector_contagion",
        "fx",
        "commodity",
        "other",
    ]
    description: Annotated[str, Field(max_length=80)]
    severity: Literal["critical", "high", "medium", "low"]

    # DeepSeek 류 모델이 news event_type (market_movement / bankruptcy 등) 을 risk
    # category 로 혼동해 반환하는 사례가 관측됨 — 알 수 없는 값은 "other" 로 강등.
    @field_validator("category", mode="before")
    @classmethod
    def _coerce_unknown_category(cls, v: object) -> object:
        if isinstance(v, str) and v not in _RISK_CATEGORIES:
            return "other"
        return v


class IndexPoint(BaseModel):
    """시장 지표 단일 포인트 (MACRO §3.3)."""

    close: float
    change_pct: float
    volume: float | None = None


class SectorDrop(BaseModel):
    """섹터 전염 체크용 — 주요 섹터 일중 하락률."""

    sector: str
    change_pct: float


class MarketSnapshot(BaseModel):
    """Macro Gate 실행 시 수집하는 시장 스냅샷 (MACRO §3.3).

    `check_closed_conditions`가 LLM과 독립적으로 참조하는 원천 데이터.
    """

    as_of: datetime

    # 국내
    kospi: IndexPoint
    kosdaq: IndexPoint
    kospi_200_futures: IndexPoint | None = None

    # 해외 (전일 종가 또는 현재 선물)
    sp500: IndexPoint
    nasdaq: IndexPoint
    nikkei: IndexPoint
    hsi: IndexPoint

    # 환율 / 원자재
    usd_krw: float
    usd_krw_change_pct: float
    usd_jpy: float
    crude_oil: float
    crude_oil_change_pct: float
    gold: float
    gold_change_pct: float

    # 공포 지수
    vix: float
    vix_prev: float | None = None
    vkospi: float | None = None

    # 변동성
    kospi_20d_vol: float
    kospi_60d_vol: float

    # 섹터 스냅샷 (check_closed_conditions에서 섹터 전염 체크)
    major_sector_drops: list[SectorDrop] = Field(default_factory=list)

    # 24h 누적 high-impact geopolitical 뉴스 건수 (news_events 기반).
    # check_closed_conditions 의 결정론 geopolitical 트리거에 사용.
    # 0 이면 미주입 또는 데이터 없음 (fail-open).
    high_impact_geopolitical_count: int = 0

    # 유동성 지표 — 거래대금 일일 비율 (today / 5d avg). 1.0 = 평소.
    # None 이면 데이터 없음 → liquidity 트리거 미평가 (fail-open).
    kospi_turnover_ratio: float | None = None
    # 호가 스프레드 일일 배수 (today / 5d avg). 평소 1.0, 2.0 이상이면 경색.
    kospi_spread_ratio: float | None = None

    @property
    def usd_krw_change_abs(self) -> float:
        return abs(self.usd_krw_change_pct)


class RecentMacroRun(BaseModel):
    """이전 Macro run 요약 (MACRO §3.4). 프롬프트 연속성 체크용."""

    macro_run_id: str
    generated_at: datetime
    gate: Literal["open", "closed"]
    size_multiplier: Annotated[float, Field(ge=0.0, le=1.0)]
    top_risks_summary: str = ""


class MacroAttemptHint(BaseModel):
    """동일 macro 호출 내 직전 시도의 실패 정보 — 재시도 시 LLM 에 주입.

    Scout 의 ``CodeAttemptHint`` 와 동일 패턴. LLM 출력 구조화 파싱 실패 / 검증
    위반 (top_risks 6개, reasoning overflow 등) 을 다음 프롬프트에 보여줘서
    같은 실수 반복을 막는다.
    """

    attempt_no: Annotated[int, Field(ge=1)]
    error: str  # "parse_error" | "validation_error" 등
    details: str  # 에러 traceback / 메시지 (truncate ok)


# =====================================================================
# Macro Context (LLM 호출 전 조립)
# =====================================================================


class MacroContext(BaseModel):
    """Macro Gate 호출 시점에 필요한 전체 입력 (MACRO §3)."""

    as_of: datetime
    trigger_reason: str  # "scheduled_0800" | "manual" | "auto_*"

    market_snapshot: MarketSnapshot

    wsj_digest_id: str  # "wsj_20260416_0730" | "unavailable"
    wsj_macro_summary: str = ""
    wsj_headlines_formatted: str = ""

    kor_macro_news_formatted: str = ""

    recent_runs: list[RecentMacroRun] = Field(default_factory=list)
    # 같은 호출 내 재시도 hint — Scout 의 previous_attempts 와 동일 패턴.
    previous_attempts: list[MacroAttemptHint] = Field(default_factory=list)


# =====================================================================
# Macro Gate 출력 (LLM structured output)
# =====================================================================


class MacroGateOutput(BaseModel):
    """Macro Gate LLM의 structured output (MACRO §2.1).

    실행 경로에 참조되는 건 오직 `gate`, `size_multiplier` 둘뿐.
    나머지는 로깅 전용 — Executor가 참조 금지 (반-어드바이저 원칙).
    """

    # 실행 로직 참조 가능
    gate: Literal["open", "closed"]
    size_multiplier: Annotated[float, Field(ge=0.0, le=1.0)]

    # 로깅 전용 — Field(max_length) 로 hard validation 걸면 초과 시 gate 가
    # closed 로 fallback 되므로 BeforeValidator 로 사전 truncate.
    reasoning: Annotated[str, BeforeValidator(lambda v: v[:2000] if isinstance(v, str) else v)]
    top_risks: Annotated[list[RiskItem], Field(max_length=5)]
    confidence: Literal["high", "medium", "low"]
    news_digest_ref: str
    next_review_hint: str | None = None
