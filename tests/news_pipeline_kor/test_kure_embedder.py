"""KureEmbedder — OpenAI-호환 응답 파싱 + 실패 fallback."""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest
import respx

from prime_jennie_runtime.news_pipeline_kor.adapters.kure_embedder import KureEmbedder
from prime_jennie_runtime.news_pipeline_kor.models import NewsArticle
from prime_jennie_runtime.news_pipeline_kor.sentiment import Embedder


def _article(title: str = "호실적", body: str = "") -> NewsArticle:
    return NewsArticle(
        article_id="a1",
        ticker="005930",
        title=title,
        body=body,
        published_at=datetime.now(),
        source_url="https://example.com/1",
        source_name="연합",
    )


@pytest.mark.asyncio
async def test_embedder_conforms_to_protocol():
    embedder = KureEmbedder(dimension=4, client=httpx.AsyncClient())
    assert isinstance(embedder, Embedder)
    await embedder.client.aclose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_embed_happy_path_returns_vector():
    vector = [0.1, 0.2, 0.3, 0.4]
    async with respx.mock(base_url="http://localhost:8002") as mock:
        mock.post("/v1/embeddings").mock(
            return_value=httpx.Response(
                200, json={"data": [{"embedding": vector}], "model": "kure-v1"}
            )
        )
        client = httpx.AsyncClient()
        embedder = KureEmbedder(dimension=4, client=client)
        result = await embedder.embed(_article())
        await client.aclose()

    assert result == vector


@pytest.mark.asyncio
async def test_embed_posts_openai_shape_body():
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"data": [{"embedding": [0.0] * 4}]})

    async with respx.mock(base_url="http://localhost:8002") as mock:
        mock.post("/v1/embeddings").mock(side_effect=_handler)
        client = httpx.AsyncClient()
        embedder = KureEmbedder(dimension=4, client=client)
        await embedder.embed(_article(title="t", body="b"))
        await client.aclose()

    import json as _json

    body = _json.loads(captured[0].content.decode())
    assert body["model"] == "kure-v1"
    assert "[005930]" in body["input"]
    assert "t" in body["input"]


@pytest.mark.asyncio
async def test_embed_http_error_returns_zero_vector():
    async with respx.mock(base_url="http://localhost:8002") as mock:
        mock.post("/v1/embeddings").mock(return_value=httpx.Response(500, text="oops"))
        client = httpx.AsyncClient()
        embedder = KureEmbedder(dimension=8, client=client)
        result = await embedder.embed(_article())
        await client.aclose()

    assert result == [0.0] * 8


@pytest.mark.asyncio
async def test_embed_parse_failure_returns_zero_vector():
    async with respx.mock(base_url="http://localhost:8002") as mock:
        mock.post("/v1/embeddings").mock(return_value=httpx.Response(200, text="not json"))
        client = httpx.AsyncClient()
        embedder = KureEmbedder(dimension=3, client=client)
        result = await embedder.embed(_article())
        await client.aclose()

    assert result == [0.0, 0.0, 0.0]


@pytest.mark.asyncio
async def test_embed_missing_data_key_fallback():
    async with respx.mock(base_url="http://localhost:8002") as mock:
        mock.post("/v1/embeddings").mock(return_value=httpx.Response(200, json={"nope": []}))
        client = httpx.AsyncClient()
        embedder = KureEmbedder(dimension=3, client=client)
        result = await embedder.embed(_article())
        await client.aclose()

    assert result == [0.0, 0.0, 0.0]


@pytest.mark.asyncio
async def test_embed_timeout_returns_zero_vector():
    async with respx.mock(base_url="http://localhost:8002") as mock:
        mock.post("/v1/embeddings").mock(side_effect=httpx.ConnectTimeout("t"))
        client = httpx.AsyncClient()
        embedder = KureEmbedder(dimension=2, client=client)
        result = await embedder.embed(_article())
        await client.aclose()

    assert result == [0.0, 0.0]
