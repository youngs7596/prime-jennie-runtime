"""QdrantVectorStore — in-memory client 로 E2E 단위 검증."""

from __future__ import annotations

from datetime import datetime

import pytest

from prime_jennie_runtime.news_pipeline_kor.adapters.qdrant_store import (
    QdrantVectorStore,
    _article_id_to_point_id,
)
from prime_jennie_runtime.news_pipeline_kor.models import NewsEmbedding
from prime_jennie_runtime.news_pipeline_kor.storage import VectorStore


def _embedding(
    article_id: str = "a1",
    ticker: str = "005930",
    vec: list[float] | None = None,
) -> NewsEmbedding:
    return NewsEmbedding(
        article_id=article_id,
        ticker=ticker,
        vector=vec or [1.0, 0.0, 0.0, 0.0],
        embedded_at=datetime.now(),
        model="kure-v1",
    )


@pytest.fixture
def store():
    return QdrantVectorStore.in_memory(dimension=4)


# ---------- Protocol ----------


@pytest.mark.asyncio
async def test_conforms_to_vectorstore_protocol(store):
    assert isinstance(store, VectorStore)


# ---------- point_id 결정성 ----------


def test_point_id_is_deterministic():
    assert _article_id_to_point_id("abc") == _article_id_to_point_id("abc")


def test_point_id_differs_by_article_id():
    assert _article_id_to_point_id("a") != _article_id_to_point_id("b")


# ---------- upsert/top_k ----------


@pytest.mark.asyncio
async def test_upsert_and_top_k_returns_self(store):
    await store.upsert(_embedding())
    results = await store.top_k([1.0, 0.0, 0.0, 0.0], ticker=None, k=5)
    assert len(results) == 1
    aid, score = results[0]
    assert aid == "a1"
    assert score > 0.99


@pytest.mark.asyncio
async def test_top_k_ranks_by_cosine(store):
    await store.upsert(_embedding("close", vec=[1.0, 0.0, 0.0, 0.0]))
    await store.upsert(_embedding("far", vec=[0.0, 1.0, 0.0, 0.0]))
    results = await store.top_k([1.0, 0.0, 0.0, 0.0], ticker=None, k=2)
    assert [aid for aid, _ in results] == ["close", "far"]


@pytest.mark.asyncio
async def test_top_k_filters_by_ticker(store):
    await store.upsert(_embedding("a", ticker="005930"))
    await store.upsert(_embedding("b", ticker="000660"))
    results = await store.top_k([1.0, 0.0, 0.0, 0.0], ticker="005930", k=10)
    assert [aid for aid, _ in results] == ["a"]


@pytest.mark.asyncio
async def test_top_k_respects_limit(store):
    for i in range(5):
        await store.upsert(_embedding(f"a{i}"))
    results = await store.top_k([1.0, 0.0, 0.0, 0.0], ticker=None, k=3)
    assert len(results) == 3


@pytest.mark.asyncio
async def test_upsert_same_article_overwrites(store):
    await store.upsert(_embedding("x", vec=[1.0, 0.0, 0.0, 0.0]))
    await store.upsert(_embedding("x", vec=[0.0, 1.0, 0.0, 0.0]))
    # orthogonal query: 두 번째 vector 가 맞으면 similarity ~ 1, 첫 번째면 ~ 0
    results = await store.top_k([0.0, 1.0, 0.0, 0.0], ticker=None, k=1)
    assert len(results) == 1
    assert results[0][1] > 0.99


@pytest.mark.asyncio
async def test_collection_name_is_v3_namespace():
    """D1 결정 회귀 — v3_* prefix 컬렉션."""
    store = QdrantVectorStore.in_memory(dimension=4)
    assert store.collection_name == "v3_news_sentiments"


@pytest.mark.asyncio
async def test_collection_created_on_first_use():
    """lazy init — 생성자는 collection 을 만들지 않음."""
    store = QdrantVectorStore.in_memory(dimension=4)
    existing = {c.name for c in store.client.get_collections().collections}
    assert "v3_news_sentiments" not in existing
    await store.upsert(_embedding())
    existing_after = {c.name for c in store.client.get_collections().collections}
    assert "v3_news_sentiments" in existing_after
