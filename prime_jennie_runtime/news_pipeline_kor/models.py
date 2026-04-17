"""News Pipeline 도메인 모델.

Phase 1 핵심: Scout가 소비할 ticker별 news_score를 만들어내는 데 필요한 최소 객체.
크롤러 / 감성 분석 / 임베딩 / 저장 단계 사이를 흐르는 데이터.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, HttpUrl

# ---------------------------------------------------------------------
# Article — 크롤링 단계 산출물
# ---------------------------------------------------------------------


class NewsArticle(BaseModel):
    """단일 뉴스 기사 (크롤러가 stream에 발행하는 형태).

    `article_id`는 dedup이 정한다 (URL hash). source_url 정규화 후 동일하면 같은 id.
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
# Sentiment — 분석 단계 산출물
# ---------------------------------------------------------------------


SentimentLabel = Literal["positive", "neutral", "negative"]


class SentimentScore(BaseModel):
    """ticker별 단일 기사에 대한 감성 분석 결과.

    score는 -1.0 ~ +1.0. label은 score를 -0.2 / +0.2로 이산화한 보조 필드.
    """

    article_id: str
    ticker: str
    score: Annotated[float, Field(ge=-1.0, le=1.0)]
    label: SentimentLabel
    analyzed_at: datetime
    model: str = "stub"  # 실제 운영에서는 "exaone-4.0-q8" 등


# ---------------------------------------------------------------------
# Embedding — 아카이빙 단계 산출물
# ---------------------------------------------------------------------


class NewsEmbedding(BaseModel):
    """기사 임베딩 + Qdrant 저장 메타데이터."""

    article_id: str
    ticker: str
    vector: list[float]
    embedded_at: datetime
    model: str = "stub-embed"  # 실 운영: "kure-v1" 등


# ---------------------------------------------------------------------
# Snapshot — Scout 공급용 ticker별 집계 결과
# ---------------------------------------------------------------------


class NewsScoreSnapshot(BaseModel):
    """ScoutContext.news_scores[ticker]가 되는 단일 ticker 스냅샷.

    NewsScoreEntry와 같은 필드 구성이지만, news_pipeline_kor 도메인 객체로 분리해
    upstream/downstream 결합도를 낮춘다 (scout_feeder가 NewsScoreEntry로 환산).
    """

    ticker: str
    score: Annotated[float, Field(ge=-1.0, le=1.0)]
    timestamp: datetime  # 가장 최신 반영 기사의 분석 시각
    article_count: Annotated[int, Field(ge=0)]
    staleness_hours: Annotated[float, Field(ge=0.0)]
