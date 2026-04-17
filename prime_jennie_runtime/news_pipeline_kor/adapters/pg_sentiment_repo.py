"""PostgresSentimentRepo — asyncpg 기반 ``SentimentRepo`` 구현.

Phase 1 ``InMemorySentimentRepo`` 와 동일 시맨틱스. migrations/004 의
``news_sentiments`` + ``news_articles`` 테이블 사용.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..models import NewsArticle, SentimentScore

logger = logging.getLogger(__name__)

_SENTIMENT_UPSERT = (
    "INSERT INTO news_sentiments "
    "(ticker, article_id, score, label, analyzed_at, model) "
    "VALUES ($1, $2, $3, $4, $5, $6) "
    "ON CONFLICT (ticker, article_id) DO UPDATE SET "
    "score=EXCLUDED.score, label=EXCLUDED.label, "
    "analyzed_at=EXCLUDED.analyzed_at, model=EXCLUDED.model, updated_at=NOW()"
)

_ARTICLE_UPSERT = (
    "INSERT INTO news_articles "
    "(article_id, ticker, title, body, published_at, source_url, source_name) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7) "
    "ON CONFLICT (article_id) DO UPDATE SET "
    "title=EXCLUDED.title, body=EXCLUDED.body, "
    "published_at=EXCLUDED.published_at, source_url=EXCLUDED.source_url, "
    "source_name=EXCLUDED.source_name"
)

_RECENT_SELECT = (
    "SELECT ticker, article_id, score, label, analyzed_at, model "
    "FROM news_sentiments "
    "WHERE ticker = $1 AND analyzed_at >= $2 "
    "ORDER BY analyzed_at DESC"
)


@dataclass
class PostgresSentimentRepo:
    """asyncpg Connection/Pool 주입. 테스트는 fake conn 으로 SQL 호환성만 검증."""

    conn: Any  # asyncpg.Connection | asyncpg.Pool

    async def upsert(self, score: SentimentScore, *, article: NewsArticle | None = None) -> None:
        await self.conn.execute(
            _SENTIMENT_UPSERT,
            score.ticker,
            score.article_id,
            float(score.score),
            score.label,
            score.analyzed_at,
            score.model,
        )
        if article is not None:
            await self.conn.execute(
                _ARTICLE_UPSERT,
                article.article_id,
                article.ticker,
                article.title,
                article.body,
                article.published_at,
                str(article.source_url),
                article.source_name,
            )

    async def recent_for_ticker(
        self,
        ticker: str,
        *,
        since: datetime,
        now: datetime | None = None,
    ) -> list[SentimentScore]:
        rows = await self.conn.fetch(_RECENT_SELECT, ticker, since)
        return [
            SentimentScore(
                ticker=r["ticker"],
                article_id=r["article_id"],
                score=float(r["score"]),
                label=r["label"],
                analyzed_at=r["analyzed_at"],
                model=r["model"],
            )
            for r in rows
        ]


__all__ = ["PostgresSentimentRepo"]
