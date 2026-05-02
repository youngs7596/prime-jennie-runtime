"""Sentiment/Event 저장소 추상화.

- SentimentRepo: 과거 호환 — news_sentiments 저장. 2026-04-21 이후 write 중단.
- EventRepo: 신규 — news_events 테이블 upsert.

Qdrant/VectorStore 계열은 2026-04-21 제거 (메타데이터 RDB 전환).

운영 어댑터:
- PostgresSentimentRepo: migrations/004 news_sentiments (legacy)
- PostgresEventRepo: migrations/014 news_events

InMemory 구현은 단위 테스트 전용.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from .models import NewsArticle, NewsEvent, SentimentScore


@runtime_checkable
class SentimentRepo(Protocol):
    """legacy ticker × article_id → SentimentScore 저장소."""

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
class EventRepo(Protocol):
    """news_events 저장소 — 2026-04-21 이후 신규 쓰기 경로."""

    async def upsert(self, event: NewsEvent, *, article: NewsArticle | None = None) -> None: ...

    async def recent_for_ticker(
        self,
        ticker: str,
        *,
        since: datetime,
        now: datetime | None = None,
    ) -> list[NewsEvent]: ...

    async def existing_article_ids(self, article_ids: list[str]) -> set[str]:
        """주어진 ids 중 이미 분석된 것들의 집합. extractor 가 LLM 호출 전 사전 필터.

        네이버가 옛 기사를 다시 노출하면 dedup TTL(3일) 만료로 stream 에 재진입하기 때문에,
        article_id PK 로 PG 사전조회해 LLM 재호출을 막는다.
        """
        ...


# ---------------------------------------------------------------------
# In-memory implementations
# ---------------------------------------------------------------------


@dataclass
class InMemorySentimentRepo:
    """append-only 저장. 같은 article_id 면 덮어씀. legacy tests 전용."""

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

    def total_count(self) -> int:
        return sum(len(b) for b in self._by_ticker.values())


@dataclass
class InMemoryEventRepo:
    """news_events 인메모리 구현. 테스트/dev."""

    _by_ticker: dict[str, dict[str, NewsEvent]] = field(default_factory=lambda: defaultdict(dict))
    _articles: dict[str, NewsArticle] = field(default_factory=dict)

    async def upsert(self, event: NewsEvent, *, article: NewsArticle | None = None) -> None:
        self._by_ticker[event.ticker][event.article_id] = event
        if article is not None:
            self._articles[article.article_id] = article

    async def recent_for_ticker(
        self,
        ticker: str,
        *,
        since: datetime,
        now: datetime | None = None,
    ) -> list[NewsEvent]:
        bucket = self._by_ticker.get(ticker, {})
        out = [e for e in bucket.values() if e.analyzed_at >= since]
        out.sort(key=lambda e: e.analyzed_at, reverse=True)
        return out

    async def existing_article_ids(self, article_ids: list[str]) -> set[str]:
        if not article_ids:
            return set()
        seen: set[str] = set()
        for bucket in self._by_ticker.values():
            seen.update(bucket.keys())
        return {aid for aid in article_ids if aid in seen}

    def total_count(self) -> int:
        return sum(len(b) for b in self._by_ticker.values())


# ---------------------------------------------------------------------
# 헬퍼: lookback window 계산
# ---------------------------------------------------------------------


def lookback_since(now: datetime, hours: float) -> datetime:
    return now - timedelta(hours=hours)
