"""NewsPipelineScoutFeeder — Track B NewsScoreFeeder Protocol 적합성 + 집계 정확."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from prime_jennie_runtime.news_pipeline_kor import (
    InMemorySentimentRepo,
    NewsPipelineScoutFeeder,
)
from prime_jennie_runtime.news_pipeline_kor.models import SentimentScore
from prime_jennie_runtime.slow_loop.scout.feeders.base import NewsScoreFeeder


def _score(ticker: str, article_id: str, score: float, when: datetime) -> SentimentScore:
    return SentimentScore(
        article_id=article_id,
        ticker=ticker,
        score=score,
        label="neutral",
        analyzed_at=when,
    )


@pytest.mark.asyncio
async def test_feeder_conforms_to_protocol():
    feeder = NewsPipelineScoutFeeder(sentiment_repo=InMemorySentimentRepo())
    assert isinstance(feeder, NewsScoreFeeder)


@pytest.mark.asyncio
async def test_empty_repo_returns_zero_entries_for_each_ticker():
    feeder = NewsPipelineScoutFeeder(sentiment_repo=InMemorySentimentRepo())
    out = await feeder.fetch(date(2026, 4, 17), ["005930", "000660"])
    assert set(out.keys()) == {"005930", "000660"}
    for entry in out.values():
        assert entry.score == 0.0
        assert entry.article_count == 0
        assert entry.staleness_hours == 24.0  # = lookback_hours 기본값


@pytest.mark.asyncio
async def test_average_score_across_recent_articles():
    repo = InMemorySentimentRepo()
    as_of = date(2026, 4, 17)
    base = datetime.combine(as_of, datetime.min.time()).replace(hour=12)
    await repo.upsert(_score("005930", "a1", 0.8, base))
    await repo.upsert(_score("005930", "a2", -0.4, base - timedelta(hours=2)))
    feeder = NewsPipelineScoutFeeder(sentiment_repo=repo)
    out = await feeder.fetch(as_of, ["005930"])
    e = out["005930"]
    assert e.article_count == 2
    assert e.score == pytest.approx((0.8 - 0.4) / 2)
    assert e.timestamp == base  # latest 분석시각


@pytest.mark.asyncio
async def test_old_articles_outside_lookback_excluded():
    repo = InMemorySentimentRepo()
    as_of = date(2026, 4, 17)
    base_now = datetime.combine(as_of, datetime.max.time()).replace(microsecond=0)
    await repo.upsert(_score("005930", "old", 1.0, base_now - timedelta(hours=48)))
    await repo.upsert(_score("005930", "new", -0.5, base_now - timedelta(hours=2)))

    feeder = NewsPipelineScoutFeeder(sentiment_repo=repo, lookback_hours=24.0)
    out = await feeder.fetch(as_of, ["005930"])
    assert out["005930"].article_count == 1
    assert out["005930"].score == -0.5


@pytest.mark.asyncio
async def test_staleness_in_hours_correct():
    repo = InMemorySentimentRepo()
    as_of = date(2026, 4, 17)
    now = datetime.combine(as_of, datetime.max.time()).replace(microsecond=0)
    # 6시간 전 분석된 기사
    await repo.upsert(_score("005930", "a", 0.0, now - timedelta(hours=6)))
    feeder = NewsPipelineScoutFeeder(sentiment_repo=repo, lookback_hours=24.0)
    out = await feeder.fetch(as_of, ["005930"])
    assert out["005930"].staleness_hours == pytest.approx(6.0, abs=0.001)


@pytest.mark.asyncio
async def test_score_clamped_to_minus1_plus1():
    """avg가 -1~+1 범위지만 향후 가중치 적용 시 안전망."""
    repo = InMemorySentimentRepo()
    as_of = date(2026, 4, 17)
    base = datetime.combine(as_of, datetime.min.time()).replace(hour=12)
    await repo.upsert(_score("005930", "a", 1.0, base))
    feeder = NewsPipelineScoutFeeder(sentiment_repo=repo)
    out = await feeder.fetch(as_of, ["005930"])
    assert -1.0 <= out["005930"].score <= 1.0


@pytest.mark.asyncio
async def test_feeder_is_per_ticker_independent():
    repo = InMemorySentimentRepo()
    as_of = date(2026, 4, 17)
    base = datetime.combine(as_of, datetime.min.time()).replace(hour=12)
    await repo.upsert(_score("005930", "x", 0.5, base))
    await repo.upsert(_score("000660", "y", -0.3, base))
    feeder = NewsPipelineScoutFeeder(sentiment_repo=repo)
    out = await feeder.fetch(as_of, ["005930", "000660", "207940"])
    assert out["005930"].score == 0.5
    assert out["000660"].score == -0.3
    assert out["207940"].article_count == 0
