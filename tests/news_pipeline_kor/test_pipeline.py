"""NewsPipeline.run_cycle — 크롤 → dedup → analyze → embed 흐름 통합."""

from __future__ import annotations

from datetime import datetime

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

UNIVERSE = ["005930", "000660"]


def _make_pipeline(
    *, with_vector: bool = True
) -> tuple[
    NewsPipeline,
    StubCrawler,
    InMemoryDeduplicator,
    InMemorySentimentRepo,
    InMemoryVectorStore | None,
]:
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


# ---------- happy path ----------


@pytest.mark.asyncio
async def test_run_cycle_processes_all_new_articles():
    pipe, crawler, _dedup, repo, vs = _make_pipeline()
    crawler.add(
        [
            make_dummy_article("005930", title="삼성전자 신고가"),
            make_dummy_article("005930", title="삼성전자 흑자"),
            make_dummy_article("000660", title="SK하이닉스 호조"),
        ]
    )
    stats = await pipe.run_cycle(UNIVERSE)
    assert stats.crawled == 3
    assert stats.deduped == 3
    assert stats.analyzed == 3
    assert stats.embedded == 3
    assert stats.errors == []
    assert repo.total_count() == 3
    assert vs is not None and len(vs) == 3


# ---------- dedup ----------


@pytest.mark.asyncio
async def test_run_cycle_skips_already_seen():
    pipe, crawler, _dedup, repo, _vs = _make_pipeline()
    a = make_dummy_article("005930", title="삼성전자 호조")
    crawler.add([a])
    stats1 = await pipe.run_cycle(UNIVERSE)
    stats2 = await pipe.run_cycle(UNIVERSE)
    assert stats1.deduped == 1
    assert stats2.deduped == 0
    assert stats2.analyzed == 0
    assert repo.total_count() == 1


# ---------- universe filter ----------


@pytest.mark.asyncio
async def test_crawler_filters_by_universe():
    pipe, crawler, _dedup, repo, _vs = _make_pipeline()
    crawler.add(
        [
            make_dummy_article("005930", title="x"),
            make_dummy_article("999999", title="y"),  # universe 밖
        ]
    )
    stats = await pipe.run_cycle(UNIVERSE)
    assert stats.crawled == 1
    assert repo.total_count() == 1


# ---------- crawler 실패 ----------


class _BoomCrawler:
    async def crawl(self, universe):
        raise RuntimeError("network down")


@pytest.mark.asyncio
async def test_run_cycle_handles_crawler_exception():
    pipe = NewsPipeline(
        crawler=_BoomCrawler(),
        deduplicator=InMemoryDeduplicator(),
        analyzer=StubSentimentAnalyzer(),
        sentiment_repo=InMemorySentimentRepo(),
    )
    stats = await pipe.run_cycle(UNIVERSE)
    assert stats.crawled == 0
    assert stats.errors and stats.errors[0].startswith("crawl:RuntimeError")


# ---------- analyzer 부분 실패 ----------


class _PartiallyBrokenAnalyzer:
    """첫 호출은 정상, 두 번째 호출은 raise."""

    def __init__(self):
        self.calls = 0

    async def analyze(self, article):
        self.calls += 1
        if self.calls == 2:
            raise ValueError("LLM 5xx")
        from prime_jennie_runtime.news_pipeline_kor.models import SentimentScore

        return SentimentScore(
            article_id=article.article_id,
            ticker=article.ticker,
            score=0.0,
            label="neutral",
            analyzed_at=datetime(2026, 4, 17, 12, 0, 0),
        )


@pytest.mark.asyncio
async def test_partial_analyzer_failure_is_logged():
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
        analyzer=_PartiallyBrokenAnalyzer(),
        sentiment_repo=repo,
    )
    stats = await pipe.run_cycle(["005930"])
    assert stats.analyzed == 2
    assert len(stats.errors) == 1
    assert stats.errors[0].startswith("analyze:")
    assert repo.total_count() == 2


# ---------- 임베딩 disabled ----------


@pytest.mark.asyncio
async def test_run_cycle_without_vector_components():
    pipe, crawler, _dedup, repo, vs = _make_pipeline(with_vector=False)
    crawler.add([make_dummy_article("005930", title="x")])
    stats = await pipe.run_cycle(UNIVERSE)
    assert stats.embedded == 0
    assert vs is None
    assert repo.total_count() == 1
