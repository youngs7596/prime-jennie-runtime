"""summarizer — fallback + DeepSeek 경로."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from prime_jennie_runtime.news_pipeline_global.models import GlobalNewsArticle
from prime_jennie_runtime.news_pipeline_global.summarizer import summarize


def _article(i: int, source: str = "wsj") -> GlobalNewsArticle:
    return GlobalNewsArticle(
        article_id=f"a{i}",
        source=source,
        feed_name=f"{source}-markets",
        title=f"Headline {i} — Fed signals rate cut",
        description=f"Summary of article {i}.",
        source_url=f"https://example.com/{i}",
        published_at=datetime(2026, 4, 18, 10, i, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_summarize_empty_returns_fallback_marker():
    summary, model = await summarize([])
    assert "없음" in summary
    assert model is None


@pytest.mark.asyncio
async def test_summarize_no_api_key_uses_fallback(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    articles = [_article(1), _article(2, source="bloomberg")]
    summary, model = await summarize(articles, api_key=None)
    assert "wsj=1" in summary or "wsj" in summary
    assert "bloomberg" in summary
    assert model is None


@pytest.mark.asyncio
async def test_summarize_calls_deepseek_and_returns_content():
    articles = [_article(1), _article(2)]
    with respx.mock() as mock:
        mock.post("https://api.deepseek.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "연준 금리 동결 시사 — 미 국채 금리 하락."}}
                    ]
                },
            )
        )
        summary, model = await summarize(
            articles,
            api_key="sk-test",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-chat",
        )
    assert "연준" in summary
    assert model == "deepseek-chat"


@pytest.mark.asyncio
async def test_summarize_deepseek_error_falls_back():
    articles = [_article(1)]
    with respx.mock() as mock:
        mock.post("https://api.deepseek.com/v1/chat/completions").mock(
            return_value=httpx.Response(500)
        )
        summary, model = await summarize(
            articles, api_key="sk-test", base_url="https://api.deepseek.com/v1"
        )
    assert model is None
    # fallback 은 소스 카운트 또는 헤드라인을 포함
    assert "wsj" in summary or "Headline" in summary
