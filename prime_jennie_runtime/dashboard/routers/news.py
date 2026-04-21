"""News API — news_events (메타데이터 추출 결과) + news_articles 조회.

2026-04-21 Track E metadata 전환 이후 control-ui News 페이지 대응.

엔드포인트:
- GET /news/events                — 목록 (필터/페이징)
- GET /news/events/{article_id}   — 단일 상세 (body + 전체 메타)
- GET /news/stats                 — event_type 분포 + top ticker (필터 옵션 +
                                    대시보드 카드 용)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import get_session

router = APIRouter(prefix="/news", tags=["news"])


# ---------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------


class NewsEventSummary(BaseModel):
    article_id: str
    ticker: str
    stock_name: str | None = None
    title: str
    source_url: str | None = None
    source_name: str | None = None
    published_at: datetime
    event_type: str
    impact_level: str
    sentiment: str
    sentiment_score: float
    time_horizon: str | None = None
    keywords: list[str] = Field(default_factory=list)
    sector_tags: list[str] = Field(default_factory=list)
    confidence: float | None = None
    analyzed_at: datetime


class NewsEventDetail(NewsEventSummary):
    body: str = ""
    financial_signals: list[dict[str, Any]] = Field(default_factory=list)
    model: str | None = None


class NewsEventsResponse(BaseModel):
    total: int
    items: list[NewsEventSummary]


class EventTypeStat(BaseModel):
    event_type: str
    count: int
    positive: int
    neutral: int
    negative: int
    high_impact: int


class TickerStat(BaseModel):
    ticker: str
    stock_name: str | None = None
    count: int
    avg_sentiment: float
    high_impact: int


class NewsStatsResponse(BaseModel):
    since_hours: float
    total_events: int
    event_types: list[EventTypeStat]
    top_tickers: list[TickerStat]


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _parse_jsonb(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return []


def _row_to_summary(row: Any) -> NewsEventSummary:
    return NewsEventSummary(
        article_id=row["article_id"],
        ticker=row["ticker"],
        stock_name=row["stock_name"],
        title=row["title"] or "",
        source_url=row["source_url"],
        source_name=row["source_name"],
        published_at=row["published_at"],
        event_type=row["event_type"],
        impact_level=row["impact_level"],
        sentiment=row["sentiment"],
        sentiment_score=float(row["sentiment_score"] or 0.0),
        time_horizon=row["time_horizon"],
        keywords=_as_list(row["keywords"]),
        sector_tags=_as_list(row["sector_tags"]),
        confidence=float(row["confidence"]) if row["confidence"] is not None else None,
        analyzed_at=row["analyzed_at"],
    )


def _since(hours: float, now: datetime | None = None) -> datetime:
    base = now or datetime.now(UTC)
    return base - timedelta(hours=hours)


# ---------------------------------------------------------------------
# GET /news/events
# ---------------------------------------------------------------------


@router.get("/events", response_model=NewsEventsResponse)
async def list_events(
    ticker: str | None = Query(None, description="정확 매칭. 6자리 숫자 코드."),
    event_type: list[str] | None = Query(None, description="복수 선택 가능."),
    impact_level: list[str] | None = Query(None, description="high/medium/low 복수 가능."),
    sentiment: list[str] | None = Query(None, description="positive/neutral/negative."),
    since_hours: float = Query(24.0, ge=0.5, le=168.0, description="최근 N 시간."),
    keyword: str | None = Query(None, description="title ILIKE 검색어 (선택)."),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> NewsEventsResponse:
    since = _since(since_hours)
    where_clauses: list[str] = ["ne.analyzed_at >= :since"]
    params: dict[str, Any] = {"since": since, "limit": limit, "offset": offset}

    if ticker:
        where_clauses.append("ne.ticker = :ticker")
        params["ticker"] = ticker
    if event_type:
        where_clauses.append("ne.event_type = ANY(:event_type)")
        params["event_type"] = list(event_type)
    if impact_level:
        where_clauses.append("ne.impact_level = ANY(:impact_level)")
        params["impact_level"] = list(impact_level)
    if sentiment:
        where_clauses.append("ne.sentiment = ANY(:sentiment)")
        params["sentiment"] = list(sentiment)
    if keyword:
        where_clauses.append("na.title LIKE :kw")
        params["kw"] = f"%{keyword}%"

    where_sql = " AND ".join(where_clauses)

    count_sql = text(
        f"""
        SELECT COUNT(*) AS n
        FROM news_events ne
        LEFT JOIN news_articles na ON na.article_id = ne.article_id
        WHERE {where_sql}
        """
    )

    list_sql = text(
        f"""
        SELECT ne.article_id, ne.ticker, sm.stock_name,
               na.title, na.source_url, na.source_name, ne.published_at,
               ne.event_type, ne.impact_level, ne.sentiment, ne.sentiment_score,
               ne.time_horizon, ne.keywords, ne.sector_tags,
               ne.confidence, ne.analyzed_at
        FROM news_events ne
        LEFT JOIN news_articles na ON na.article_id = ne.article_id
        LEFT JOIN stock_masters sm ON sm.stock_code = ne.ticker
        WHERE {where_sql}
        ORDER BY
          CASE ne.impact_level WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
          ne.analyzed_at DESC
        LIMIT :limit OFFSET :offset
        """
    )

    total_row = (await session.execute(count_sql, params)).first()
    rows = (await session.execute(list_sql, params)).mappings().all()

    return NewsEventsResponse(
        total=int(total_row[0]) if total_row else 0,
        items=[_row_to_summary(r) for r in rows],
    )


# ---------------------------------------------------------------------
# GET /news/events/{article_id}
# ---------------------------------------------------------------------


@router.get("/events/{article_id}", response_model=NewsEventDetail)
async def get_event(
    article_id: str,
    session: AsyncSession = Depends(get_session),
) -> NewsEventDetail:
    sql = text(
        """
        SELECT ne.article_id, ne.ticker, sm.stock_name,
               na.title, na.body, na.source_url, na.source_name, ne.published_at,
               ne.event_type, ne.impact_level, ne.sentiment, ne.sentiment_score,
               ne.time_horizon, ne.keywords, ne.sector_tags, ne.financial_signals,
               ne.confidence, ne.model, ne.analyzed_at
        FROM news_events ne
        LEFT JOIN news_articles na ON na.article_id = ne.article_id
        LEFT JOIN stock_masters sm ON sm.stock_code = ne.ticker
        WHERE ne.article_id = :aid
        """
    )
    row = (await session.execute(sql, {"aid": article_id})).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"article_id={article_id} not found")

    base = _row_to_summary(row).model_dump()
    return NewsEventDetail(
        **base,
        body=row["body"] or "",
        financial_signals=_parse_jsonb(row["financial_signals"]) or [],
        model=row["model"],
    )


# ---------------------------------------------------------------------
# GET /news/stats
# ---------------------------------------------------------------------


@router.get("/stats", response_model=NewsStatsResponse)
async def news_stats(
    since_hours: float = Query(24.0, ge=0.5, le=168.0),
    top_n: int = Query(10, ge=1, le=50, description="top ticker 개수"),
    session: AsyncSession = Depends(get_session),
) -> NewsStatsResponse:
    since = _since(since_hours)

    total_sql = text("SELECT COUNT(*) AS n FROM news_events WHERE analyzed_at >= :since")
    total_row = (await session.execute(total_sql, {"since": since})).first()
    total = int(total_row[0]) if total_row else 0

    event_type_sql = text(
        """
        SELECT event_type,
               COUNT(*) AS n,
               SUM(CASE WHEN sentiment = 'positive' THEN 1 ELSE 0 END) AS pos,
               SUM(CASE WHEN sentiment = 'neutral' THEN 1 ELSE 0 END) AS neu,
               SUM(CASE WHEN sentiment = 'negative' THEN 1 ELSE 0 END) AS neg,
               SUM(CASE WHEN impact_level = 'high' THEN 1 ELSE 0 END) AS high_imp
        FROM news_events
        WHERE analyzed_at >= :since
        GROUP BY event_type
        ORDER BY n DESC
        """
    )
    et_rows = (await session.execute(event_type_sql, {"since": since})).mappings().all()
    event_types = [
        EventTypeStat(
            event_type=r["event_type"],
            count=int(r["n"]),
            positive=int(r["pos"] or 0),
            neutral=int(r["neu"] or 0),
            negative=int(r["neg"] or 0),
            high_impact=int(r["high_imp"] or 0),
        )
        for r in et_rows
    ]

    ticker_sql = text(
        """
        SELECT ne.ticker, sm.stock_name,
               COUNT(*) AS n,
               AVG(ne.sentiment_score) AS avg_s,
               SUM(CASE WHEN ne.impact_level = 'high' THEN 1 ELSE 0 END) AS high_imp
        FROM news_events ne
        LEFT JOIN stock_masters sm ON sm.stock_code = ne.ticker
        WHERE ne.analyzed_at >= :since
        GROUP BY ne.ticker, sm.stock_name
        ORDER BY n DESC
        LIMIT :n
        """
    )
    t_rows = (await session.execute(ticker_sql, {"since": since, "n": top_n})).mappings().all()
    top_tickers = [
        TickerStat(
            ticker=r["ticker"],
            stock_name=r["stock_name"],
            count=int(r["n"]),
            avg_sentiment=float(r["avg_s"] or 0.0),
            high_impact=int(r["high_imp"] or 0),
        )
        for r in t_rows
    ]

    return NewsStatsResponse(
        since_hours=since_hours,
        total_events=total,
        event_types=event_types,
        top_tickers=top_tickers,
    )
