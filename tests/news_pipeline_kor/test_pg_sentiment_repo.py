"""PostgresSentimentRepo — fake asyncpg conn 으로 SQL/바인드 파라미터 검증.

실 Postgres 는 CI 가용성 이슈로 scope 밖. 대신 호출 SQL 문자열 + 파라미터가
스키마(migrations/004)와 일치하는지 회귀 검증.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pytest

from prime_jennie_runtime.news_pipeline_kor.adapters.pg_sentiment_repo import (
    PostgresSentimentRepo,
)
from prime_jennie_runtime.news_pipeline_kor.models import NewsArticle, SentimentScore
from prime_jennie_runtime.news_pipeline_kor.storage import SentimentRepo


@dataclass
class FakeAsyncpgConn:
    """execute / fetch 호출을 녹음. rows 는 미리 주입."""

    execute_calls: list[tuple[str, tuple]] = field(default_factory=list)
    fetch_calls: list[tuple[str, tuple]] = field(default_factory=list)
    rows_to_return: list[dict] = field(default_factory=list)

    async def execute(self, sql: str, *args: Any) -> str:
        self.execute_calls.append((sql, args))
        return "INSERT 0 1"

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        self.fetch_calls.append((sql, args))
        return list(self.rows_to_return)


def _score(
    ticker: str = "005930",
    article_id: str = "a1",
    score: float = 0.5,
    analyzed_at: datetime | None = None,
) -> SentimentScore:
    return SentimentScore(
        ticker=ticker,
        article_id=article_id,
        score=score,
        label="positive" if score > 0.2 else ("negative" if score < -0.2 else "neutral"),
        analyzed_at=analyzed_at or datetime.now(),
        model="kure-v1",
    )


def _article(article_id: str = "a1", ticker: str = "005930") -> NewsArticle:
    return NewsArticle(
        article_id=article_id,
        ticker=ticker,
        title="호실적",
        body="본문",
        published_at=datetime.now(),
        source_url="https://example.com/1",
        source_name="연합",
    )


# ---------- Protocol ----------


def test_conforms_to_sentimentrepo_protocol():
    repo = PostgresSentimentRepo(FakeAsyncpgConn())
    assert isinstance(repo, SentimentRepo)


# ---------- upsert(score only) ----------


@pytest.mark.asyncio
async def test_upsert_score_calls_sentiment_upsert():
    conn = FakeAsyncpgConn()
    repo = PostgresSentimentRepo(conn)
    await repo.upsert(_score())

    assert len(conn.execute_calls) == 1
    sql, params = conn.execute_calls[0]
    assert "INSERT INTO news_sentiments" in sql
    assert "ON CONFLICT (ticker, article_id) DO UPDATE" in sql
    # 6 개 파라미터: ticker, article_id, score, label, analyzed_at, model
    assert len(params) == 6
    assert params[0] == "005930"
    assert params[1] == "a1"


@pytest.mark.asyncio
async def test_upsert_without_article_skips_article_insert():
    conn = FakeAsyncpgConn()
    repo = PostgresSentimentRepo(conn)
    await repo.upsert(_score())
    # 오직 sentiment upsert 1 회
    assert len(conn.execute_calls) == 1


# ---------- upsert(score + article) ----------


@pytest.mark.asyncio
async def test_upsert_with_article_calls_both_tables():
    conn = FakeAsyncpgConn()
    repo = PostgresSentimentRepo(conn)
    await repo.upsert(_score(), article=_article())
    assert len(conn.execute_calls) == 2
    sentiment_sql = conn.execute_calls[0][0]
    article_sql = conn.execute_calls[1][0]
    assert "news_sentiments" in sentiment_sql
    assert "INSERT INTO news_articles" in article_sql
    assert "ON CONFLICT (article_id) DO UPDATE" in article_sql


@pytest.mark.asyncio
async def test_upsert_article_params_includes_source_url_as_string():
    conn = FakeAsyncpgConn()
    repo = PostgresSentimentRepo(conn)
    await repo.upsert(_score(), article=_article())
    article_params = conn.execute_calls[1][1]
    # article 파라미터 7개: article_id, ticker, title, body, published_at, source_url, source_name
    assert len(article_params) == 7
    assert article_params[5].startswith("https://")  # source_url 은 str 로 변환


# ---------- recent_for_ticker ----------


@pytest.mark.asyncio
async def test_recent_for_ticker_binds_ticker_and_since():
    conn = FakeAsyncpgConn()
    conn.rows_to_return = [
        {
            "ticker": "005930",
            "article_id": "a1",
            "score": 0.4,
            "label": "positive",
            "analyzed_at": datetime(2026, 4, 17, 10, 0, 0),
            "model": "kure-v1",
        }
    ]
    since = datetime(2026, 4, 16, 0, 0, 0)
    repo = PostgresSentimentRepo(conn)
    results = await repo.recent_for_ticker("005930", since=since)

    assert len(conn.fetch_calls) == 1
    sql, params = conn.fetch_calls[0]
    assert "WHERE ticker = $1 AND analyzed_at >= $2" in sql
    assert params == ("005930", since)
    assert len(results) == 1
    assert results[0].ticker == "005930"
    assert results[0].score == 0.4


@pytest.mark.asyncio
async def test_recent_for_ticker_empty_result():
    conn = FakeAsyncpgConn()
    repo = PostgresSentimentRepo(conn)
    results = await repo.recent_for_ticker("999999", since=datetime.now() - timedelta(hours=24))
    assert results == []


@pytest.mark.asyncio
async def test_recent_for_ticker_preserves_row_fields():
    conn = FakeAsyncpgConn()
    conn.rows_to_return = [
        {
            "ticker": "000660",
            "article_id": "z",
            "score": -0.7,
            "label": "negative",
            "analyzed_at": datetime(2026, 4, 17, 10, 0, 0),
            "model": "exaone",
        }
    ]
    repo = PostgresSentimentRepo(conn)
    [score] = await repo.recent_for_ticker("000660", since=datetime(2026, 4, 1))
    assert score.label == "negative"
    assert score.model == "exaone"
    assert score.score == pytest.approx(-0.7)
