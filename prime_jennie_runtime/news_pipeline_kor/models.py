"""News Pipeline 도메인 모델.

2026-04-21 이후 메타데이터 추출 기반으로 전환 — ``NewsEvent`` 가 신규 분석 결과.
``SentimentScore`` 는 legacy 호환 + Scout news_score 계산에 여전히 사용.
``NewsEmbedding`` 은 Qdrant 제거로 함께 삭제.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, HttpUrl

# ---------------------------------------------------------------------
# Article — 크롤링 단계 산출물
# ---------------------------------------------------------------------


class NewsArticle(BaseModel):
    """단일 뉴스 기사 (크롤러가 stream 에 발행하는 형태).

    ``article_id`` 는 dedup 이 정한다 (URL hash). source_url 정규화 후 동일하면 같은 id.
    """

    article_id: str
    ticker: str
    title: str
    body: str
    published_at: datetime
    source_url: HttpUrl
    source_name: str = ""
    fetched_at: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------
# Sentiment — legacy 분석 결과 (write 중단, read-only 이력 유지)
# ---------------------------------------------------------------------


SentimentLabel = Literal["positive", "neutral", "negative"]


class SentimentScore(BaseModel):
    """legacy ticker × 기사 → 감성. 2026-04-21 이후 write 안함.

    score 는 -1.0 ~ +1.0. label 은 ±0.2 임계로 이산화.
    """

    article_id: str
    ticker: str
    score: Annotated[float, Field(ge=-1.0, le=1.0)]
    label: SentimentLabel
    analyzed_at: datetime
    model: str = "stub"


# ---------------------------------------------------------------------
# Event — 신규 메타데이터 추출 결과
# ---------------------------------------------------------------------


EventType = Literal[
    "earnings",
    "mna",
    "lawsuit",
    "product",
    "personnel",
    "regulation",
    "contract",
    "strike",
    "other",
]
ImpactLevel = Literal["high", "medium", "low"]
TimeHorizon = Literal["immediate", "short", "medium", "long"]
SignalDirection = Literal["up", "down", "flat"]


class FinancialSignal(BaseModel):
    type: str  # revenue/profit/guidance/dividend/margin/...
    direction: SignalDirection


class NewsEvent(BaseModel):
    """EXAONE 메타데이터 추출 결과 — migrations/014 news_events 에 upsert."""

    article_id: str
    ticker: str
    published_at: datetime

    event_type: EventType
    impact_level: ImpactLevel
    sentiment: SentimentLabel
    sentiment_score: Annotated[float, Field(ge=-1.0, le=1.0)] = 0.0
    time_horizon: TimeHorizon = "short"

    keywords: list[str] = Field(default_factory=list, max_length=10)
    sector_tags: list[str] = Field(default_factory=list, max_length=5)
    financial_signals: list[FinancialSignal] = Field(default_factory=list)
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.5

    model: str = "stub"
    analyzed_at: datetime


# ---------------------------------------------------------------------
# Snapshot — Scout 공급용 ticker별 집계 결과 (호환 유지)
# ---------------------------------------------------------------------


class NewsScoreSnapshot(BaseModel):
    """ScoutContext.news_scores[ticker] 로 흘러가는 ticker 스냅샷.

    NewsScoreEntry 와 같은 필드지만 도메인 분리를 위해 별도 모델 — scout_feeder
    가 NewsScoreEntry 로 환산.
    """

    ticker: str
    score: Annotated[float, Field(ge=-1.0, le=1.0)]
    timestamp: datetime
    article_count: Annotated[int, Field(ge=0)]
    staleness_hours: Annotated[float, Field(ge=0.0)]
