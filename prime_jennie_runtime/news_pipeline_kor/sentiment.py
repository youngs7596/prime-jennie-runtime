"""감성 분석 추상화 (Phase 1).

v2 운영: EXAONE 4.0 Q8 (vLLM 호스팅). Phase 1 Track E는 Protocol + 결정론 Stub.
실제 LLM 어댑터는 Phase 2에서 LiteLLM 경유로 합쳐서 추가.

스코어 산식 (Stub):
- 긍정 키워드 hit 수 - 부정 키워드 hit 수
- ÷ (긍정 + 부정 키워드 총량 + 1)  ← 0/0 회피, 0~±1 클램프
- label은 -0.2/+0.2 임계값으로 이산화
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from .models import NewsArticle, SentimentLabel, SentimentScore


@runtime_checkable
class SentimentAnalyzer(Protocol):
    """단일 기사 → SentimentScore. 배치는 호출자가 비동기 gather로."""

    async def analyze(self, article: NewsArticle) -> SentimentScore: ...


# Phase 1 결정론 stub — 운영 EXAONE 대체
_DEFAULT_POSITIVE = (
    "상승",
    "호조",
    "신고가",
    "흑자",
    "수주",
    "급등",
    "어닝서프라이즈",
    "최대",
    "확대",
)
_DEFAULT_NEGATIVE = (
    "하락",
    "급락",
    "적자",
    "손실",
    "감소",
    "리콜",
    "악재",
    "하향",
    "둔화",
)


def _label_for(
    score: float, *, pos_threshold: float = 0.2, neg_threshold: float = -0.2
) -> SentimentLabel:
    if score >= pos_threshold:
        return "positive"
    if score <= neg_threshold:
        return "negative"
    return "neutral"


@dataclass
class StubSentimentAnalyzer:
    """키워드 기반 결정론 분석. 같은 입력 → 같은 출력 (테스트 재현성)."""

    positive_keywords: tuple[str, ...] = _DEFAULT_POSITIVE
    negative_keywords: tuple[str, ...] = _DEFAULT_NEGATIVE
    model_name: str = "stub-keyword-v1"

    async def analyze(self, article: NewsArticle) -> SentimentScore:
        text = f"{article.title}\n{article.body}"
        pos = sum(1 for kw in self.positive_keywords if kw in text)
        neg = sum(1 for kw in self.negative_keywords if kw in text)

        denom = pos + neg + 1
        raw = (pos - neg) / denom
        # ±1 클램프 (이미 -1~+1이지만 명시적으로)
        score = max(-1.0, min(1.0, raw))

        return SentimentScore(
            article_id=article.article_id,
            ticker=article.ticker,
            score=score,
            label=_label_for(score),
            analyzed_at=datetime.now(),
            model=self.model_name,
        )


# ---------------------------------------------------------------------
# 임베더 — Track E의 Qdrant 아카이빙 단계용 추상화
# ---------------------------------------------------------------------


@runtime_checkable
class Embedder(Protocol):
    """기사 텍스트 → 벡터. 운영: kure-v1 (768d). 테스트: 결정론 stub."""

    dimension: int

    async def embed(self, article: NewsArticle) -> list[float]: ...


@dataclass
class StubEmbedder:
    """ticker + 기사 length 기반 결정론 벡터. 같은 article_id → 같은 vector."""

    dimension: int = 8
    seed_offset: float = 0.0
    _cache: dict[str, list[float]] = field(default_factory=dict)

    async def embed(self, article: NewsArticle) -> list[float]:
        if article.article_id in self._cache:
            return self._cache[article.article_id]
        # 단순 결정론: article_id의 ord 합으로 vector 채움
        seed = sum(ord(c) for c in article.article_id)
        vec = [((seed + i) % 100) / 100.0 + self.seed_offset for i in range(self.dimension)]
        self._cache[article.article_id] = vec
        return vec
