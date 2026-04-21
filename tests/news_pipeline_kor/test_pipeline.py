"""NewsPipeline — v2 3-stage Redis Stream 구조 (collect/analyze/archive).

v2 ``prime_jennie/services/news/`` 3-thread 모델을 v3 asyncio 로 이식한 동작을 검증.
"""

from __future__ import annotations

from datetime import datetime

import fakeredis
import pytest

from prime_jennie_runtime.news_pipeline_kor import (
    InMemoryDeduplicator,
    InMemorySentimentRepo,
    InMemoryVectorStore,
    NewsPipeline,
    StubCrawler,
    StubEmbedder,
    StubSentimentAnalyzer,
    make_dummy_article,
)
from prime_jennie_runtime.news_pipeline_kor.models import SentimentScore
from prime_jennie_runtime.news_pipeline_kor.stream import (
    ANALYZER_GROUP,
    ARCHIVER_GROUP,
    NEWS_STREAM,
)

UNIVERSE = ["005930", "000660"]


def _pipeline(*, with_vector: bool = True):
    crawler = StubCrawler()
    dedup = InMemoryDeduplicator()
    repo = InMemorySentimentRepo()
    vs: InMemoryVectorStore | None = InMemoryVectorStore() if with_vector else None
    pipe = NewsPipeline(
        crawler=crawler,
        deduplicator=dedup,
        analyzer=StubSentimentAnalyzer(),
        sentiment_repo=repo,
        embedder=StubEmbedder() if with_vector else None,
        vector_store=vs,
    )
    return pipe, crawler, dedup, repo, vs


def _fake_redis() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=False)


# ---------- collect_and_publish ----------


@pytest.mark.asyncio
async def test_collect_publishes_new_articles_to_stream():
    pipe, crawler, _dedup, _repo, _vs = _pipeline()
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
    assert stats.deduped == 3  # 발행된 신규 건수
    # Stream 에 3 건 쌓여야
    assert r.xlen(NEWS_STREAM) == 3


@pytest.mark.asyncio
async def test_collect_skips_dedup_on_second_call():
    pipe, crawler, _dedup, _repo, _vs = _pipeline()
    r = _fake_redis()
    crawler.add([make_dummy_article("005930", title="삼성 호조")])
    stats1 = await pipe.collect_and_publish(UNIVERSE, r)
    stats2 = await pipe.collect_and_publish(UNIVERSE, r)
    assert stats1.deduped == 1
    assert stats2.deduped == 0  # dedup 이 걸러내 stream 재발행 안함
    assert r.xlen(NEWS_STREAM) == 1


@pytest.mark.asyncio
async def test_collect_filters_universe_via_crawler():
    pipe, crawler, _dedup, _repo, _vs = _pipeline()
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
    """per-ticker 루프라 universe 의 ticker 각각에 대해 에러가 기록되지만 cycle 은
    계속 진행 — 단일 ticker 장애가 전체 cycle 을 내리지 않아야."""

    class _Boom:
        async def crawl(self, universe):
            raise RuntimeError("network down")

    pipe = NewsPipeline(
        crawler=_Boom(),
        deduplicator=InMemoryDeduplicator(),
        analyzer=StubSentimentAnalyzer(),
        sentiment_repo=InMemorySentimentRepo(),
    )
    r = _fake_redis()
    stats = await pipe.collect_and_publish(UNIVERSE, r)
    assert stats.crawled == 0
    # universe 의 ticker 수 만큼 에러 누적
    assert len(stats.errors) == len(UNIVERSE)
    assert stats.errors[0].startswith("crawl:")
    assert "RuntimeError" in stats.errors[0]
    assert r.xlen(NEWS_STREAM) == 0


@pytest.mark.asyncio
async def test_collect_one_ticker_failure_does_not_abort_cycle():
    """한 ticker 가 실패해도 나머지 ticker 는 그대로 발행."""

    class _FlakyCrawler:
        def __init__(self):
            self._fail_next = False

        async def crawl(self, universe):
            ticker = universe[0]
            if ticker == "000660":
                raise RuntimeError("boom")
            return [make_dummy_article(ticker, title=f"{ticker} ok")]

    pipe = NewsPipeline(
        crawler=_FlakyCrawler(),
        deduplicator=InMemoryDeduplicator(),
        analyzer=StubSentimentAnalyzer(),
        sentiment_repo=InMemorySentimentRepo(),
    )
    r = _fake_redis()
    stats = await pipe.collect_and_publish(["005930", "000660", "035720"], r)
    assert stats.crawled == 2  # 005930, 035720
    assert stats.deduped == 2
    assert len(stats.errors) == 1
    assert stats.errors[0].startswith("crawl:000660:")
    assert r.xlen(NEWS_STREAM) == 2


# ---------- analyze_stream_once ----------


@pytest.mark.asyncio
async def test_analyze_consumes_stream_and_upserts():
    pipe, crawler, _dedup, repo, _vs = _pipeline()
    r = _fake_redis()
    crawler.add(
        [
            make_dummy_article("005930", title="삼성전자 신고가 흑자"),
            make_dummy_article("000660", title="SK하이닉스 호조 상승"),
        ]
    )
    await pipe.collect_and_publish(UNIVERSE, r)
    assert r.xlen(NEWS_STREAM) == 2

    stats = await pipe.analyze_stream_once(r)
    assert stats.analyzed == 2
    assert repo.total_count() == 2

    # 모두 ACK 되어 pending 0
    pending = r.xpending(NEWS_STREAM, ANALYZER_GROUP)
    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_analyze_partial_failure_still_acks_all():
    """분석 중 일부 실패해도 XACK. v2 동일한 at-most-once 정책."""

    class _PartiallyBroken:
        def __init__(self):
            self.calls = 0

        async def analyze(self, article):
            self.calls += 1
            if self.calls == 2:
                raise ValueError("LLM 5xx")
            return SentimentScore(
                article_id=article.article_id,
                ticker=article.ticker,
                score=0.0,
                label="neutral",
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
    repo = InMemorySentimentRepo()
    pipe = NewsPipeline(
        crawler=crawler,
        deduplicator=InMemoryDeduplicator(),
        analyzer=_PartiallyBroken(),
        sentiment_repo=repo,
    )
    r = _fake_redis()
    await pipe.collect_and_publish(["005930"], r)
    stats = await pipe.analyze_stream_once(r)
    assert stats.analyzed == 2
    assert len(stats.errors) == 1
    assert stats.errors[0].startswith("analyze:")
    assert repo.total_count() == 2
    pending = r.xpending(NEWS_STREAM, ANALYZER_GROUP)
    assert pending["pending"] == 0  # 실패 포함 모두 ACK


@pytest.mark.asyncio
async def test_analyze_on_empty_stream_returns_zero():
    pipe, _crawler, _dedup, repo, _vs = _pipeline()
    r = _fake_redis()
    stats = await pipe.analyze_stream_once(r)
    assert stats.analyzed == 0
    assert repo.total_count() == 0


# ---------- archive_stream_once ----------


@pytest.mark.asyncio
async def test_archive_consumes_independently_from_analyzer():
    """Analyzer group 과 Archiver group 이 같은 메시지를 각자 소비."""
    pipe, crawler, _dedup, repo, vs = _pipeline()
    r = _fake_redis()
    crawler.add([make_dummy_article("005930", title="x")])
    await pipe.collect_and_publish(["005930"], r)

    a_stats = await pipe.analyze_stream_once(r)
    b_stats = await pipe.archive_stream_once(r)

    assert a_stats.analyzed == 1
    assert b_stats.embedded == 1
    assert repo.total_count() == 1
    assert vs is not None and len(vs) == 1

    assert r.xpending(NEWS_STREAM, ANALYZER_GROUP)["pending"] == 0
    assert r.xpending(NEWS_STREAM, ARCHIVER_GROUP)["pending"] == 0


@pytest.mark.asyncio
async def test_archive_skipped_when_no_vector_components():
    pipe, crawler, _dedup, _repo, vs = _pipeline(with_vector=False)
    r = _fake_redis()
    crawler.add([make_dummy_article("005930", title="x")])
    await pipe.collect_and_publish(["005930"], r)
    stats = await pipe.archive_stream_once(r)
    assert stats.embedded == 0
    assert vs is None


# pending 복구 경로는 fakeredis 가 XREADGROUP last_id="0" 반환을 실제 Redis 와
# 다르게 구현하기 때문에 유닛 테스트로는 검증 불가. 실제 운영에선 v2 동일 패턴으로
# 동작. 해당 분기는 _read_group 의 얇은 fall-through 로직이라 main ">" 경로가
# 커버됨.
