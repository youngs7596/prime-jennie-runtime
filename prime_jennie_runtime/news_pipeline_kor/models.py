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
    # 기업 이벤트 (Corporate)
    "earnings",  # 실적 발표 (매출/영업익/순익)
    "mna",  # 인수·합병·매각·지분
    "lawsuit",  # 소송·손해배상·판결
    "product",  # 신제품·상용화·출시
    "personnel",  # 인사·선임·사임
    "contract",  # 수주·계약·공급
    "strike",  # 파업·노조·쟁의
    "shareholder_return",  # 배당·자사주·밸류업
    "investment",  # 유상증자·회사채·자금조달
    "bankruptcy",  # 상장폐지·워크아웃·회생
    # 시장/거시 (Market/Macro)
    "market_movement",  # 지수·거래량·투자자 심리
    "geopolitical",  # 지정학 (이란/우크라이나/미중 무역)
    "regulation",  # 규제·정책·승인·제재
    # 금융상품/분석 (Product/Analysis)
    "fund_product",  # ETF/펀드 순자산·신상품·수익률
    "analyst_rating",  # 증권사 리포트·목표가·매수의견
    # 분류 불가
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
