"""Macro Gate 입출력 Pydantic 스키마.

MACRO_GATE_SPEC §2~§3 구현.
- MacroGateOutput: LLM structured output (gate, size_multiplier, 로깅용 필드)
- MarketSnapshot: 지수/환율/변동성 스냅샷 (결정론 closed 조건 판정에도 사용)
- MacroContext: Macro Gate 프롬프트 구성에 쓰는 입력
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

# =====================================================================
# 보조 타입
# =====================================================================


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

    # 로깅 전용
    reasoning: Annotated[str, Field(max_length=500)]
    top_risks: Annotated[list[RiskItem], Field(max_length=5)]
    confidence: Literal["high", "medium", "low"]
    news_digest_ref: str
    next_review_hint: str | None = None
