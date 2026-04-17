"""SentimentRepo + VectorStore (InMemory) — upsert / lookback / cosine top-k."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from prime_jennie_runtime.news_pipeline_kor.crawler import make_dummy_article
from prime_jennie_runtime.news_pipeline_kor.models import (
    NewsEmbedding,
    SentimentScore,
)
from prime_jennie_runtime.news_pipeline_kor.storage import (
    InMemorySentimentRepo,
    InMemoryVectorStore,
    lookback_since,
)


def _score(ticker: str, article_id: str, *, score: float, when: datetime) -> SentimentScore:
    return SentimentScore(
        article_id=article_id,
        ticker=ticker,
        score=score,
        label="neutral",
        analyzed_at=when,
    )


# ---------- SentimentRepo ----------


@pytest.mark.asyncio
async def test_repo_upsert_and_recent():
    repo = InMemorySentimentRepo()
    t = datetime(2026, 4, 17, 12, 0, 0)
    await repo.upsert(_score("005930", "a1", score=0.5, when=t))
    await repo.upsert(_score("005930", "a2", score=-0.2, when=t - timedelta(hours=10)))
    await repo.upsert(_score("000660", "b1", score=0.3, when=t))

    out = await repo.recent_for_ticker("005930", since=t - timedelta(hours=24), now=t)
    assert len(out) == 2
    # 정렬 — 최신 우선
    assert out[0].article_id == "a1"


@pytest.mark.asyncio
async def test_repo_recent_excludes_old():
    repo = InMemorySentimentRepo()
    t = datetime(2026, 4, 17, 12, 0, 0)
    await repo.upsert(_score("005930", "old", score=0.0, when=t - timedelta(hours=48)))
    out = await repo.recent_for_ticker("005930", since=t - timedelta(hours=24), now=t)
    assert out == []


@pytest.mark.asyncio
async def test_repo_upsert_overwrites_same_id():
    repo = InMemorySentimentRepo()
    t = datetime(2026, 4, 17, 12, 0, 0)
    await repo.upsert(_score("005930", "a1", score=0.1, when=t))
    await repo.upsert(_score("005930", "a1", score=0.9, when=t))
    out = await repo.recent_for_ticker("005930", since=t - timedelta(hours=1), now=t)
    assert len(out) == 1
    assert out[0].score == 0.9


# ---------- VectorStore ----------


def _emb(article_id: str, ticker: str, vec: list[float]) -> NewsEmbedding:
    return NewsEmbedding(
        article_id=article_id,
        ticker=ticker,
        vector=vec,
        embedded_at=datetime(2026, 4, 17),
    )


@pytest.mark.asyncio
async def test_vector_store_top_k_orders_by_cosine():
    vs = InMemoryVectorStore()
    await vs.upsert(_emb("a", "005930", [1.0, 0.0, 0.0]))
    await vs.upsert(_emb("b", "005930", [0.7, 0.7, 0.0]))
    await vs.upsert(_emb("c", "005930", [0.0, 1.0, 0.0]))

    top = await vs.top_k([1.0, 0.0, 0.0], ticker="005930", k=2)
    assert [aid for aid, _ in top] == ["a", "b"]


@pytest.mark.asyncio
async def test_vector_store_filters_by_ticker():
    vs = InMemoryVectorStore()
    await vs.upsert(_emb("a", "005930", [1.0, 0.0]))
    await vs.upsert(_emb("b", "000660", [1.0, 0.0]))
    top_only_a = await vs.top_k([1.0, 0.0], ticker="005930", k=10)
    assert len(top_only_a) == 1


@pytest.mark.asyncio
async def test_vector_store_zero_vector_returns_zero_similarity():
    vs = InMemoryVectorStore()
    await vs.upsert(_emb("a", "005930", [0.0, 0.0]))
    top = await vs.top_k([1.0, 0.0], ticker=None, k=5)
    assert top[0][1] == 0.0


# ---------- lookback_since ----------


def test_lookback_since_subtracts_hours():
    t = datetime(2026, 4, 17, 12, 0, 0)
    s = lookback_since(t, hours=24.0)
    assert s == t - timedelta(hours=24)


# ---------- Article meta storage ----------


@pytest.mark.asyncio
async def test_repo_can_attach_article_metadata():
    """upsert에 article을 넘기면 별도 dict에 저장 (RAG 용 등 추후 활용)."""
    repo = InMemorySentimentRepo()
    t = datetime(2026, 4, 17, 12, 0, 0)
    art = make_dummy_article("005930", title="title")
    sc = _score("005930", art.article_id, score=0.0, when=t)
    await repo.upsert(sc, article=art)
    # 내부 dict 확인
    assert art.article_id in repo._articles
