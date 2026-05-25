"""pipeline.build_digest — LLM 호출 시 record_llm_call 누적 검증."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import fakeredis.aioredis
import httpx
import pytest

from prime_jennie_runtime.news_pipeline_global import pipeline
from prime_jennie_runtime.news_pipeline_global.models import GlobalNewsArticle


def _article(i: int) -> GlobalNewsArticle:
    return GlobalNewsArticle(
        article_id=f"a{i}",
        source="wsj",
        feed_name="wsj-markets",
        title=f"Headline {i}",
        description=f"Body {i}",
        source_url=f"https://example.com/{i}",
        published_at=datetime(2026, 4, 18, 10, i, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_build_digest_records_llm_call_when_summarize_uses_llm(monkeypatch):
    """summarize 가 model + usage 를 돌려주면 news_global_digest 키에 누적."""
    captured: dict[str, Any] = {}

    async def fake_fetch(_pool, _since, limit=50):
        return [_article(1), _article(2)]

    async def fake_upsert(_pool, digest):
        captured["digest"] = digest

    async def fake_summarize(articles, **kwargs):
        return "요약", "deepseek-chat", {"input_tokens": 1000, "output_tokens": 200}

    monkeypatch.setattr(pipeline, "fetch_recent_articles", fake_fetch)
    monkeypatch.setattr(pipeline, "upsert_digest", fake_upsert)
    monkeypatch.setattr(pipeline, "summarize", fake_summarize)

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    as_of = datetime(2026, 5, 25, 7, 30, tzinfo=UTC)
    await pipeline.build_digest(
        pool=None,  # fake_fetch / fake_upsert 가 무시
        http=httpx.AsyncClient(),
        as_of=as_of,
        redis_client=redis,
    )

    # KST 16:30 → 2026-05-25 키 (as_of UTC 07:30 + 9h)
    keys = sorted(await redis.keys("llm:stats:*"))
    assert keys == ["llm:stats:2026-05-25:news_global_digest"]
    data = await redis.hgetall(keys[0])
    assert int(data["calls"]) == 1
    assert int(data["tokens_in"]) == 1000
    assert int(data["tokens_out"]) == 200
    # DeepSeek chat 가격: (1000*0.27 + 200*1.10) / 1M
    assert abs(float(data["cost_usd"]) - 0.00049) < 1e-7


@pytest.mark.asyncio
async def test_build_digest_skips_record_on_fallback(monkeypatch):
    """summarize 가 model=None (fallback) 이면 record_llm_call 하지 않음."""

    async def fake_fetch(_pool, _since, limit=50):
        return [_article(1)]

    async def fake_upsert(_pool, _digest):
        return None

    async def fake_summarize(articles, **kwargs):
        return "(fallback)", None, None

    monkeypatch.setattr(pipeline, "fetch_recent_articles", fake_fetch)
    monkeypatch.setattr(pipeline, "upsert_digest", fake_upsert)
    monkeypatch.setattr(pipeline, "summarize", fake_summarize)

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await pipeline.build_digest(
        pool=None,
        http=httpx.AsyncClient(),
        redis_client=redis,
    )

    assert await redis.keys("llm:stats:*") == []
