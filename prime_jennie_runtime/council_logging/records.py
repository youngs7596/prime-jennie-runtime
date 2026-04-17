"""Macro Council 실행 레코드.

v2 `CouncilInput`/`CouncilResult` + `DailyMacroInsightDB.raw_council_output_json` 을
하나의 레코드로 통합. v3 단일-step `MacroGate` 결과도 `chief_judge` 필드에 요약 형태로
담을 수 있도록 모든 step 필드는 optional.

포팅 원본:
- prime-jennie/prime_jennie/services/council/pipeline.py::CouncilInput, CouncilResult
- prime-jennie/prime_jennie/infra/database/models.py::DailyMacroInsightDB
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass
class CouncilStepOutput:
    """3-step pipeline 1개 step 의 raw 결과.

    v2 pipeline.py 의 step 별 dict 를 그대로 보존. v3 단일-step 운영 시
    `chief_judge` 하나만 채워도 유효.
    """

    name: str  # "strategist" | "risk_analyst" | "chief_judge" | "macro_gate"
    output: dict[str, Any] = field(default_factory=dict)
    model_used: str | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    prompt_version: str | None = None


@dataclass
class CouncilRunRecord:
    """Macro Council 실행 1회를 표현하는 레코드.

    macro_runs 테이블의 주요 필드 + metadata_json 에 보존되는 확장 필드.
    """

    # macro_runs 주요 필드
    macro_run_id: str  # macro_YYYYMMDD_HHMM
    generated_at: datetime
    trigger_reason: str  # scheduled_0800 | manual | auto_*
    gate: str  # open | closed
    size_multiplier: float
    reasoning: str | None = None
    top_risks: list[dict[str, Any]] = field(default_factory=list)
    confidence: str | None = None  # high | medium | low
    news_digest_ref: str | None = None
    next_review_hint: str | None = None
    prompt_version: str | None = None
    model_used: str | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    auto_override: bool = False

    # 확장 — metadata_json 에 병합 저장
    target_date: date | None = None
    steps: list[CouncilStepOutput] = field(default_factory=list)
    council_consensus: str | None = None  # strong_agree | agree | partial_disagree | disagree
    trading_reasoning: str = ""
    strategies_to_favor: list[str] = field(default_factory=list)
    strategies_to_avoid: list[str] = field(default_factory=list)
    sectors_to_favor: list[str] = field(default_factory=list)
    sectors_to_avoid: list[str] = field(default_factory=list)
    opportunity_factors: list[str] = field(default_factory=list)
    risk_factors: list[str] = field(default_factory=list)

    # 입력 컨텍스트 (replay 용) — 원문 브리핑/스냅샷 요약
    input_briefing_text: str = ""
    input_global_snapshot: dict[str, Any] = field(default_factory=dict)
    input_political_news: list[str] = field(default_factory=list)
    input_sector_momentum_text: str = ""
    input_index_technical_text: str = ""

    # 상위 집계
    final_sentiment: str | None = None  # bullish | ... | bearish
    final_sentiment_score: int | None = None
    final_regime_hint: str | None = None
    final_position_size_pct: int | None = None
    final_stop_loss_adjust_pct: int | None = None
    political_risk_level: str | None = None

    success: bool = True
    error: str = ""

    def step_by_name(self, name: str) -> CouncilStepOutput | None:
        for s in self.steps:
            if s.name == name:
                return s
        return None

    def total_cost_usd(self) -> float:
        """steps 비용 합산 — cost_usd 미기록시 fallback 으로 사용."""
        if self.cost_usd is not None:
            return float(self.cost_usd)
        return sum((s.cost_usd or 0.0) for s in self.steps)
