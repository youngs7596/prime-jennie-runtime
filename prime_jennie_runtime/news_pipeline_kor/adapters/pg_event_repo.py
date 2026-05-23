"""PostgresEventRepo — asyncpg 기반 ``EventRepo`` 구현.

migrations/014 ``news_events`` 테이블 + 기존 ``news_articles`` (004) 를 사용.
financial_signals 는 jsonb 로 저장.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..models import NewsArticle, NewsEvent

logger = logging.getLogger(__name__)

_EVENT_UPSERT = (
    "INSERT INTO news_events "
    "(article_id, ticker, published_at, event_type, impact_level, sentiment, "
    " sentiment_score, time_horizon, keywords, sector_tags, financial_signals, "
    " confidence, shadow_metadata, model, analyzed_at) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, $13::jsonb, $14, $15) "
    "ON CONFLICT (article_id) DO UPDATE SET "
    "ticker=EXCLUDED.ticker, "
    "published_at=EXCLUDED.published_at, "
    "event_type=EXCLUDED.event_type, "
    "impact_level=EXCLUDED.impact_level, "
    "sentiment=EXCLUDED.sentiment, "
    "sentiment_score=EXCLUDED.sentiment_score, "
    "time_horizon=EXCLUDED.time_horizon, "
    "keywords=EXCLUDED.keywords, "
    "sector_tags=EXCLUDED.sector_tags, "
    "financial_signals=EXCLUDED.financial_signals, "
    "confidence=EXCLUDED.confidence, "
    "shadow_metadata=COALESCE(EXCLUDED.shadow_metadata, news_events.shadow_metadata), "
    "model=EXCLUDED.model, "
    "analyzed_at=EXCLUDED.analyzed_at, "
    "updated_at=NOW()"
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
    "SELECT article_id, ticker, published_at, event_type, impact_level, sentiment, "
    "       sentiment_score, time_horizon, keywords, sector_tags, "
    "       financial_signals, confidence, model, analyzed_at "
    "FROM news_events "
    "WHERE ticker = $1 AND analyzed_at >= $2 "
    "ORDER BY analyzed_at DESC"
)

_EXISTING_IDS_SELECT = "SELECT article_id FROM news_events WHERE article_id = ANY($1::text[])"


@dataclass
class PostgresEventRepo:
    """asyncpg Connection/Pool 주입."""

    conn: Any  # asyncpg.Connection | asyncpg.Pool

    async def upsert(self, event: NewsEvent, *, article: NewsArticle | None = None) -> None:
        # news_articles 가 FK 대상이므로 event upsert 전에 article 먼저 upsert.
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

        signals_json = json.dumps([s.model_dump() for s in event.financial_signals])
        shadow_json = (
            json.dumps(event.shadow_metadata) if event.shadow_metadata is not None else None
        )
        await self.conn.execute(
            _EVENT_UPSERT,
            event.article_id,
            event.ticker,
            event.published_at,
            event.event_type,
            event.impact_level,
            event.sentiment,
            float(event.sentiment_score),
            event.time_horizon,
            list(event.keywords),
            list(event.sector_tags),
            signals_json,
            float(event.confidence),
            shadow_json,
            event.model,
            event.analyzed_at,
        )

    async def existing_article_ids(self, article_ids: list[str]) -> set[str]:
        if not article_ids:
            return set()
        rows = await self.conn.fetch(_EXISTING_IDS_SELECT, article_ids)
        return {r["article_id"] for r in rows}

    async def recent_for_ticker(
        self,
        ticker: str,
        *,
        since: datetime,
        now: datetime | None = None,
    ) -> list[NewsEvent]:
        rows = await self.conn.fetch(_RECENT_SELECT, ticker, since)
        out: list[NewsEvent] = []
        for r in rows:
            raw_signals = r["financial_signals"] or []
            # asyncpg 가 jsonb 를 list/dict 로 decode (이미 parsing 됨)
            if isinstance(raw_signals, str):
                raw_signals = json.loads(raw_signals)
            out.append(
                NewsEvent(
                    article_id=r["article_id"],
                    ticker=r["ticker"],
                    published_at=r["published_at"],
                    event_type=r["event_type"],
                    impact_level=r["impact_level"],
                    sentiment=r["sentiment"],
                    sentiment_score=float(r["sentiment_score"] or 0.0),
                    time_horizon=r["time_horizon"] or "short",
                    keywords=list(r["keywords"] or []),
                    sector_tags=list(r["sector_tags"] or []),
                    financial_signals=raw_signals,  # FinancialSignal coerce via pydantic
                    confidence=float(r["confidence"] or 0.0),
                    model=r["model"],
                    analyzed_at=r["analyzed_at"],
                )
            )
        return out


__all__ = ["PostgresEventRepo"]
