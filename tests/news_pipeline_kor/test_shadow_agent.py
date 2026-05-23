"""NewsAgentShadow 단위 테스트 — completion_fn 주입으로 LLM 격리."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import pytest
from pydantic import HttpUrl

from prime_jennie_runtime.news_pipeline_kor.models import NewsArticle
from prime_jennie_runtime.news_pipeline_kor.shadow_agent import (
    NewsAgentShadow,
    ShadowResult,
)


def _make_article(title: str = "삼성물산 압구정4구역 시공권 확보", body: str = "") -> NewsArticle:
    return NewsArticle(
        article_id="abc12345",
        ticker="028260",
        title=title,
        body=body,
        published_at=datetime(2026, 5, 23, 17, 0),
        source_url=HttpUrl("https://example.com/news/1"),
        source_name="TEST",
    )


def _fake_completion(reply: str) -> Callable[..., Awaitable[Any]]:
    async def _fn(**_kwargs: Any) -> dict:
        return {"choices": [{"message": {"content": reply}}]}

    return _fn


@pytest.mark.asyncio
async def test_extract_returns_tickers_and_rationales() -> None:
    payload = {
        "tickers": ["삼성물산"],
        "ticker_rationales": {"삼성물산": "압구정4구역 시공권 획득이 주제"},
        "is_market_general": False,
        "confidence": 0.92,
    }
    agent = NewsAgentShadow(completion_fn=_fake_completion(json.dumps(payload)))
    result = await agent.extract(_make_article())
    assert isinstance(result, ShadowResult)
    assert result.tickers == ["삼성물산"]
    assert result.ticker_rationales == {"삼성물산": "압구정4구역 시공권 획득이 주제"}
    assert result.is_market_general is False
    assert result.confidence == pytest.approx(0.92)


@pytest.mark.asyncio
async def test_extract_market_general_empty_tickers() -> None:
    payload = {
        "tickers": [],
        "ticker_rationales": {},
        "is_market_general": True,
        "confidence": 0.95,
    }
    agent = NewsAgentShadow(completion_fn=_fake_completion(json.dumps(payload)))
    result = await agent.extract(_make_article("코스피 7200선 마감, 외국인 6조 순매도"))
    assert result is not None
    assert result.tickers == []
    assert result.is_market_general is True


@pytest.mark.asyncio
async def test_extract_parse_failure_returns_none() -> None:
    agent = NewsAgentShadow(completion_fn=_fake_completion("cannot extract"))
    result = await agent.extract(_make_article())
    assert result is None


@pytest.mark.asyncio
async def test_extract_completion_exception_returns_none() -> None:
    async def _boom(**_kwargs: Any) -> Any:
        raise RuntimeError("vllm down")

    agent = NewsAgentShadow(completion_fn=_boom)
    result = await agent.extract(_make_article())
    assert result is None


@pytest.mark.asyncio
async def test_rationales_filtered_by_allowed_tickers() -> None:
    # rationales 에 emit ticker 와 다른 키가 있으면 drop.
    payload = {
        "tickers": ["삼성물산"],
        "ticker_rationales": {
            "삼성물산": "유효",
            "신한지주": "제거되어야 함",  # tickers 에 없으니 drop
        },
        "is_market_general": False,
        "confidence": 0.8,
    }
    agent = NewsAgentShadow(completion_fn=_fake_completion(json.dumps(payload)))
    result = await agent.extract(_make_article())
    assert result is not None
    assert result.ticker_rationales == {"삼성물산": "유효"}


@pytest.mark.asyncio
async def test_to_metadata_round_trip() -> None:
    result = ShadowResult(
        tickers=["삼성전자"],
        ticker_rationales={"삼성전자": "주제"},
        is_market_general=False,
        confidence=0.9,
        model="qwen3-test",
        analyzed_at=datetime(2026, 5, 23, 10, 0),
    )
    meta = result.to_metadata()
    assert meta["tickers"] == ["삼성전자"]
    assert meta["is_market_general"] is False
    assert meta["confidence"] == 0.9
    assert meta["model"] == "qwen3-test"
    assert meta["analyzed_at"] == "2026-05-23T10:00:00"


@pytest.mark.asyncio
async def test_confidence_clamped() -> None:
    payload = {
        "tickers": ["삼성전자"],
        "ticker_rationales": {},
        "is_market_general": False,
        "confidence": 1.5,  # > 1.0 → clamped
    }
    agent = NewsAgentShadow(completion_fn=_fake_completion(json.dumps(payload)))
    result = await agent.extract(_make_article())
    assert result is not None
    assert result.confidence == 1.0


@pytest.mark.asyncio
async def test_extract_handles_markdown_fence() -> None:
    payload = {
        "tickers": ["삼성전자"],
        "ticker_rationales": {},
        "is_market_general": False,
        "confidence": 0.7,
    }
    fenced = f"```json\n{json.dumps(payload)}\n```"
    agent = NewsAgentShadow(completion_fn=_fake_completion(fenced))
    result = await agent.extract(_make_article())
    assert result is not None
    assert result.tickers == ["삼성전자"]
