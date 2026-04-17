"""Sentiment 저장소 + Vector store 추상화.

Phase 1 Track E:
- SentimentRepo: ticker별 SentimentScore + 기사 메타. Scout feeder가 read.
- VectorStore: Qdrant 추상화. Phase 1은 InMemory만, Phase 2에서 httpx adapter.

운영(Phase 2+) 어댑터:
- PostgresSentimentRepo: v3 schema의 news_sentiments 테이블
- QdrantVectorStore: kure-v1 임베딩 + 컬렉션 검색

InMemory 구현은 단위 테스트와 single-process dev에서 충분.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from .models import NewsArticle, NewsEmbedding, SentimentScore


@runtime_checkable
class SentimentRepo(Protocol):
    """ticker별 (article_id → SentimentScore) 저장소."""

    async def upsert(
        self, score: SentimentScore, *, article: NewsArticle | None = None
    ) -> None: ...

    async def recent_for_ticker(
        self,
        ticker: str,
        *,
        since: datetime,
        now: datetime | None = None,
    ) -> list[SentimentScore]: ...


@runtime_checkable
class VectorStore(Protocol):
    """기사 임베딩 upsert + ticker별 top-k 검색."""

    async def upsert(self, embedding: NewsEmbedding) -> None: ...

    async def top_k(
        self,
        query_vector: list[float],
        *,
        ticker: str | None,
        k: int,
    ) -> list[tuple[str, float]]:
        """``[(article_id, similarity)]`` (코사인 또는 L2-역수). 구현 자유."""
        ...


# ---------------------------------------------------------------------
# In-memory implementations
# ---------------------------------------------------------------------


@dataclass
class InMemorySentimentRepo:
    """append-only 저장. 같은 article_id가 들어오면 덮어씀 (재분석 시 최신 우선)."""

    _by_ticker: dict[str, dict[str, SentimentScore]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    _articles: dict[str, NewsArticle] = field(default_factory=dict)

    async def upsert(self, score: SentimentScore, *, article: NewsArticle | None = None) -> None:
        self._by_ticker[score.ticker][score.article_id] = score
        if article is not None:
            self._articles[article.article_id] = article

    async def recent_for_ticker(
        self,
        ticker: str,
        *,
        since: datetime,
        now: datetime | None = None,
    ) -> list[SentimentScore]:
        bucket = self._by_ticker.get(ticker, {})
        out = [s for s in bucket.values() if s.analyzed_at >= since]
        out.sort(key=lambda s: s.analyzed_at, reverse=True)
        return out

    # 테스트 보조
    def total_count(self) -> int:
        return sum(len(b) for b in self._by_ticker.values())


@dataclass
class InMemoryVectorStore:
    """간단한 코사인 유사도 검색."""

    _store: dict[str, NewsEmbedding] = field(default_factory=dict)

    async def upsert(self, embedding: NewsEmbedding) -> None:
        self._store[embedding.article_id] = embedding

    async def top_k(
        self,
        query_vector: list[float],
        *,
        ticker: str | None,
        k: int,
    ) -> list[tuple[str, float]]:
        scored: list[tuple[str, float]] = []
        for emb in self._store.values():
            if ticker is not None and emb.ticker != ticker:
                continue
            scored.append((emb.article_id, _cosine(query_vector, emb.vector)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]

    def __len__(self) -> int:
        return len(self._store)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ---------------------------------------------------------------------
# 헬퍼: lookback window 계산
# ---------------------------------------------------------------------


def lookback_since(now: datetime, hours: float) -> datetime:
    return now - timedelta(hours=hours)
