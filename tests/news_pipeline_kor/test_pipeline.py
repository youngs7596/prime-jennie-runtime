"""NewsPipeline — collect_and_publish / extract_stream_once 동작 검증.

v2 3-stage 모델을 2 stage (collector/extractor) 로 단순화한 뒤. Qdrant/archiver
제거 + EXAONE metadata 추출 전환.
"""

from __future__ import annotations

from datetime import datetime

import fakeredis
import pytest

from prime_jennie_runtime.news_pipeline_kor import (
    InMemoryDeduplicator,
    InMemoryEventRepo,
    NewsPipeline,
    StubCrawler,
    StubEventExtractor,
    make_dummy_article,
)
from prime_jennie_runtime.news_pipeline_kor.models import NewsEvent
from prime_jennie_runtime.news_pipeline_kor.stream import (
    EXTRACTOR_GROUP,
    NEWS_STREAM,
)

UNIVERSE = ["005930", "000660"]


def _pipeline():
    crawler = StubCrawler()
    dedup = InMemoryDeduplicator()
    repo = InMemoryEventRepo()
    pipe = NewsPipeline(
        crawler=crawler,
        deduplicator=dedup,
        extractor=StubEventExtractor(),
        event_repo=repo,
    )
    return pipe, crawler, dedup, repo


def _fake_redis() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=False)


# ---------- collect_and_publish ----------


@pytest.mark.asyncio
async def test_collect_publishes_new_articles_to_stream():
    pipe, crawler, _dedup, _repo = _pipeline()
    r = _fake_redis()
    crawler.add(
        [
            make_dummy_article("005930", title="삼성전자 신고가"),
            make_dummy_article("005930", title="삼성전자 흑자"),
            make_dummy_article("000660", title="SK하이닉스 호조"),
        ]
    )
    stats = await pipe.collect_and_publish(UNIVERSE, r)
    assert stats.crawled == 3
    assert stats.deduped == 3
    assert r.xlen(NEWS_STREAM) == 3


@pytest.mark.asyncio
async def test_collect_skips_dedup_on_second_call():
    pipe, crawler, _dedup, _repo = _pipeline()
    r = _fake_redis()
    crawler.add([make_dummy_article("005930", title="삼성 호조")])
    stats1 = await pipe.collect_and_publish(UNIVERSE, r)
    stats2 = await pipe.collect_and_publish(UNIVERSE, r)
    assert stats1.deduped == 1
    assert stats2.deduped == 0
    assert r.xlen(NEWS_STREAM) == 1


@pytest.mark.asyncio
async def test_collect_filters_universe_via_crawler():
    pipe, crawler, _dedup, _repo = _pipeline()
    r = _fake_redis()
    crawler.add(
        [
            make_dummy_article("005930", title="x"),
            make_dummy_article("999999", title="y"),
        ]
    )
    stats = await pipe.collect_and_publish(UNIVERSE, r)
    assert stats.crawled == 1
    assert r.xlen(NEWS_STREAM) == 1


@pytest.mark.asyncio
async def test_collect_handles_crawler_exception():
    class _Boom:
        async def crawl(self, universe):
            raise RuntimeError("network down")

    pipe = NewsPipeline(
        crawler=_Boom(),
        deduplicator=InMemoryDeduplicator(),
        extractor=StubEventExtractor(),
        event_repo=InMemoryEventRepo(),
    )
    r = _fake_redis()
    stats = await pipe.collect_and_publish(UNIVERSE, r)
    assert stats.crawled == 0
    assert len(stats.errors) == len(UNIVERSE)
    assert stats.errors[0].startswith("crawl:")
    assert "RuntimeError" in stats.errors[0]
    assert r.xlen(NEWS_STREAM) == 0


@pytest.mark.asyncio
async def test_collect_one_ticker_failure_does_not_abort_cycle():
    class _FlakyCrawler:
        async def crawl(self, universe):
            ticker = universe[0]
            if ticker == "000660":
                raise RuntimeError("boom")
            return [make_dummy_article(ticker, title=f"{ticker} ok")]

    pipe = NewsPipeline(
        crawler=_FlakyCrawler(),
        deduplicator=InMemoryDeduplicator(),
        extractor=StubEventExtractor(),
        event_repo=InMemoryEventRepo(),
    )
    r = _fake_redis()
    stats = await pipe.collect_and_publish(["005930", "000660", "035720"], r)
    assert stats.crawled == 2
    assert stats.deduped == 2
    assert len(stats.errors) == 1
    assert stats.errors[0].startswith("crawl:000660:")
    assert r.xlen(NEWS_STREAM) == 2


# ---------- extract_stream_once ----------


@pytest.mark.asyncio
async def test_extract_consumes_stream_and_upserts_events():
    pipe, crawler, _dedup, repo = _pipeline()
    r = _fake_redis()
    crawler.add(
        [
            make_dummy_article("005930", title="삼성전자 매출 영업이익 어닝서프라이즈"),
            make_dummy_article("000660", title="SK하이닉스 수주 계약 체결"),
        ]
    )
    await pipe.collect_and_publish(UNIVERSE, r)
    assert r.xlen(NEWS_STREAM) == 2

    stats = await pipe.extract_stream_once(r)
    assert stats.extracted == 2
    assert repo.total_count() == 2

    pending = r.xpending(NEWS_STREAM, EXTRACTOR_GROUP)
    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_extract_partial_failure_still_acks_all():
    """추출기 실패해도 XACK — v2 at-most-once 정책."""

    class _PartiallyBroken:
        def __init__(self):
            self.calls = 0

        async def extract(self, article):
            self.calls += 1
            if self.calls == 2:
                raise ValueError("LLM 5xx")
            return NewsEvent(
                article_id=article.article_id,
                ticker=article.ticker,
                published_at=article.published_at,
                event_type="other",
                impact_level="low",
                sentiment="neutral",
                sentiment_score=0.0,
                time_horizon="short",
                keywords=[],
                sector_tags=[],
                financial_signals=[],
                confidence=0.5,
                model="stub",
                analyzed_at=datetime(2026, 4, 21, 12, 0, 0),
            )

    crawler = StubCrawler()
    crawler.add(
        [
            make_dummy_article("005930", title="a"),
            make_dummy_article("005930", title="b"),
            make_dummy_article("005930", title="c"),
        ]
    )
    repo = InMemoryEventRepo()
    pipe = NewsPipeline(
        crawler=crawler,
        deduplicator=InMemoryDeduplicator(),
        extractor=_PartiallyBroken(),
        event_repo=repo,
    )
    r = _fake_redis()
    await pipe.collect_and_publish(["005930"], r)
    stats = await pipe.extract_stream_once(r)
    assert stats.extracted == 2
    assert len(stats.errors) == 1
    assert stats.errors[0].startswith("extract:")
    assert repo.total_count() == 2
    assert r.xpending(NEWS_STREAM, EXTRACTOR_GROUP)["pending"] == 0


@pytest.mark.asyncio
async def test_extract_on_empty_stream_returns_zero():
    pipe, _crawler, _dedup, repo = _pipeline()
    r = _fake_redis()
    stats = await pipe.extract_stream_once(r)
    assert stats.extracted == 0
    assert repo.total_count() == 0
