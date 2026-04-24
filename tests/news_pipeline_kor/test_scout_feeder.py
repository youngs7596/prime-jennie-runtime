"""NewsPipelineScoutFeeder — event_repo 기반. Track B NewsEventFeeder 적합성 + 집계."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from prime_jennie_runtime.news_pipeline_kor import (
    InMemoryEventRepo,
    NewsPipelineScoutFeeder,
)
from prime_jennie_runtime.news_pipeline_kor.models import NewsEvent
from prime_jennie_runtime.slow_loop.scout.feeders.base import NewsEventFeeder


def _event(
    ticker: str,
    article_id: str,
    sentiment_score: float,
    analyzed_at: datetime,
    *,
    event_type: str = "other",
    impact_level: str = "medium",
) -> NewsEvent:
    return NewsEvent(
        article_id=article_id,
        ticker=ticker,
        published_at=analyzed_at,
        event_type=event_type,  # type: ignore[arg-type]
        impact_level=impact_level,  # type: ignore[arg-type]
        sentiment="neutral",
        sentiment_score=sentiment_score,
        time_horizon="short",
        keywords=[],
        sector_tags=[],
        financial_signals=[],
        confidence=0.5,
        model="stub",
        analyzed_at=analyzed_at,
    )


@pytest.mark.asyncio
async def test_feeder_conforms_to_protocol():
    feeder = NewsPipelineScoutFeeder(event_repo=InMemoryEventRepo())
    assert isinstance(feeder, NewsEventFeeder)


@pytest.mark.asyncio
async def test_empty_repo_returns_zero_entries_for_each_ticker():
    feeder = NewsPipelineScoutFeeder(event_repo=InMemoryEventRepo())
    out = await feeder.fetch(date(2026, 4, 17), ["005930", "000660"])
    assert set(out.keys()) == {"005930", "000660"}
    for entry in out.values():
        assert entry.article_count == 0
        assert entry.latest_at is None
        assert entry.staleness_hours == 24.0
        assert entry.events_by_impact == {}
        assert entry.positive_events == []
        assert entry.risk_events == []


@pytest.mark.asyncio
async def test_events_by_impact_aggregates_correctly():
    repo = InMemoryEventRepo()
    as_of = date(2026, 4, 17)
    base = datetime.combine(as_of, datetime.min.time()).replace(hour=12)
    await repo.upsert(_event("005930", "a1", 0.8, base, event_type="earnings", impact_level="high"))
    await repo.upsert(_event("005930", "a2", 0.5, base, event_type="earnings", impact_level="high"))
    await repo.upsert(
        _event("005930", "a3", 0.0, base, event_type="product", impact_level="medium")
    )
    feeder = NewsPipelineScoutFeeder(event_repo=repo)
    out = await feeder.fetch(as_of, ["005930"])
    e = out["005930"]
    assert e.article_count == 3
    assert e.latest_at == base
    assert e.events_by_impact == {
        "high": {"earnings": 2},
        "medium": {"product": 1},
    }
    # earnings 는 POSITIVE 카테고리.
    assert e.positive_events == ["earnings"]
    assert e.risk_events == []


@pytest.mark.asyncio
async def test_risk_event_classification():
    repo = InMemoryEventRepo()
    as_of = date(2026, 4, 17)
    base = datetime.combine(as_of, datetime.min.time()).replace(hour=12)
    await repo.upsert(
        _event("005930", "r1", -0.5, base, event_type="regulation", impact_level="high")
    )
    await repo.upsert(_event("005930", "r2", -0.6, base, event_type="strike", impact_level="high"))
    feeder = NewsPipelineScoutFeeder(event_repo=repo)
    out = await feeder.fetch(as_of, ["005930"])
    assert out["005930"].risk_events == ["regulation", "strike"]
    assert out["005930"].positive_events == []


@pytest.mark.asyncio
async def test_old_events_outside_lookback_excluded():
    repo = InMemoryEventRepo()
    as_of = date(2026, 4, 17)
    base_now = datetime.combine(as_of, datetime.max.time()).replace(microsecond=0)
    await repo.upsert(_event("005930", "old", 1.0, base_now - timedelta(hours=48)))
    await repo.upsert(_event("005930", "new", -0.5, base_now - timedelta(hours=2)))

    feeder = NewsPipelineScoutFeeder(event_repo=repo, lookback_hours=24.0)
    out = await feeder.fetch(as_of, ["005930"])
    assert out["005930"].article_count == 1


@pytest.mark.asyncio
async def test_staleness_in_hours_correct():
    repo = InMemoryEventRepo()
    as_of = date(2026, 4, 17)
    now = datetime.combine(as_of, datetime.max.time()).replace(microsecond=0)
    await repo.upsert(_event("005930", "a", 0.0, now - timedelta(hours=6)))
    feeder = NewsPipelineScoutFeeder(event_repo=repo, lookback_hours=24.0)
    out = await feeder.fetch(as_of, ["005930"])
    assert out["005930"].staleness_hours == pytest.approx(6.0, abs=0.001)


@pytest.mark.asyncio
async def test_feeder_is_per_ticker_independent():
    repo = InMemoryEventRepo()
    as_of = date(2026, 4, 17)
    base = datetime.combine(as_of, datetime.min.time()).replace(hour=12)
    await repo.upsert(_event("005930", "x", 0.5, base, event_type="earnings", impact_level="high"))
    await repo.upsert(
        _event("000660", "y", -0.3, base, event_type="regulation", impact_level="high")
    )
    feeder = NewsPipelineScoutFeeder(event_repo=repo)
    out = await feeder.fetch(as_of, ["005930", "000660", "207940"])
    assert out["005930"].positive_events == ["earnings"]
    assert out["000660"].risk_events == ["regulation"]
    assert out["207940"].article_count == 0
