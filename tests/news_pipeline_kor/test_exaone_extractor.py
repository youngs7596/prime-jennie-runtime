"""LiteLLMEventExtractor — completion_fn 주입으로 LiteLLM 없이 단위 검증."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from prime_jennie_runtime.news_pipeline_kor.adapters.exaone_extractor import (
    LiteLLMEventExtractor,
    _parse_event_json,
)
from prime_jennie_runtime.news_pipeline_kor.models import NewsArticle
from prime_jennie_runtime.news_pipeline_kor.sentiment import EventExtractor


def _article(title: str = "삼성전자 어닝서프라이즈", body: str = "") -> NewsArticle:
    return NewsArticle(
        article_id="a1",
        ticker="005930",
        title=title,
        body=body,
        published_at=datetime(2026, 4, 21, 9, 0, 0),
        source_url="https://example.com/1",
        source_name="연합",
    )


def _fake_completion(content: str):
    async def _fn(**kwargs: Any) -> dict:
        return {"choices": [{"message": {"content": content}}]}

    return _fn


_FULL_JSON = {
    "event_type": "earnings",
    "impact_level": "high",
    "sentiment": "positive",
    "sentiment_score": 0.8,
    "time_horizon": "short",
    "keywords": ["어닝서프라이즈", "매출"],
    "sector_tags": ["반도체"],
    "financial_signals": [{"type": "revenue", "direction": "up"}],
    "confidence": 0.9,
}


# ---------- _parse_event_json ----------


def test_parse_plain_json():
    assert _parse_event_json(json.dumps(_FULL_JSON)) == _FULL_JSON


def test_parse_in_code_fence():
    raw = f"```json\n{json.dumps(_FULL_JSON)}\n```"
    assert _parse_event_json(raw) == _FULL_JSON


def test_parse_with_surrounding_text():
    raw = f"Here is: {json.dumps(_FULL_JSON)} end."
    result = _parse_event_json(raw)
    assert result == _FULL_JSON


def test_parse_garbage_returns_none():
    assert _parse_event_json("not json at all") is None
    assert _parse_event_json("") is None


# ---------- extract: happy path ----------


@pytest.mark.asyncio
async def test_extract_full_structured_response():
    extractor = LiteLLMEventExtractor(completion_fn=_fake_completion(json.dumps(_FULL_JSON)))
    event = await extractor.extract(_article())
    assert event.event_type == "earnings"
    assert event.impact_level == "high"
    assert event.sentiment == "positive"
    assert event.sentiment_score == pytest.approx(0.8)
    assert event.time_horizon == "short"
    assert "어닝서프라이즈" in event.keywords
    assert event.sector_tags == ["반도체"]
    assert len(event.financial_signals) == 1
    assert event.financial_signals[0].type == "revenue"
    assert event.financial_signals[0].direction == "up"
    assert event.confidence == pytest.approx(0.9)
    assert event.ticker == "005930"
    assert event.article_id == "a1"


@pytest.mark.asyncio
async def test_extract_coerces_invalid_enum_to_defaults():
    bad = {
        "event_type": "invented_type",
        "impact_level": "cataclysmic",
        "sentiment": "????",
        "sentiment_score": 5.0,
        "time_horizon": "nope",
        "confidence": 2.0,
    }
    extractor = LiteLLMEventExtractor(completion_fn=_fake_completion(json.dumps(bad)))
    event = await extractor.extract(_article())
    assert event.event_type == "other"
    assert event.impact_level == "low"
    assert event.sentiment == "neutral"
    assert event.sentiment_score == 1.0
    assert event.time_horizon == "short"
    assert event.confidence == 1.0


# ---------- extract: 실패 fallback ----------


@pytest.mark.asyncio
async def test_extract_unparseable_returns_fallback():
    extractor = LiteLLMEventExtractor(completion_fn=_fake_completion("I cannot extract this."))
    event = await extractor.extract(_article())
    assert event.event_type == "other"
    assert event.impact_level == "low"
    assert event.sentiment == "neutral"
    assert event.sentiment_score == 0.0
    assert event.confidence == 0.0


@pytest.mark.asyncio
async def test_extract_completion_raises_returns_fallback():
    async def _boom(**kwargs: Any) -> dict:
        raise RuntimeError("rate limit")

    extractor = LiteLLMEventExtractor(completion_fn=_boom)
    event = await extractor.extract(_article())
    assert event.event_type == "other"
    assert event.impact_level == "low"


@pytest.mark.asyncio
async def test_extract_passes_model_to_completion_fn():
    captured: dict[str, Any] = {}

    async def _capture(**kwargs: Any) -> dict:
        captured.update(kwargs)
        return {"choices": [{"message": {"content": json.dumps(_FULL_JSON)}}]}

    extractor = LiteLLMEventExtractor(
        model="openai/exaone",
        api_base="http://localhost:8001/v1",
        completion_fn=_capture,
    )
    await extractor.extract(_article(title="t", body="b"))
    assert captured["model"] == "openai/exaone"
    assert captured["api_base"] == "http://localhost:8001/v1"
    prompt = captured["messages"][0]["content"]
    assert "event_type" in prompt
    assert "sentiment_score" in prompt


@pytest.mark.asyncio
async def test_extractor_conforms_to_protocol():
    extractor = LiteLLMEventExtractor(completion_fn=_fake_completion(json.dumps(_FULL_JSON)))
    assert isinstance(extractor, EventExtractor)


@pytest.mark.asyncio
async def test_extract_handles_object_style_response():
    class _Msg:
        content = json.dumps(_FULL_JSON)

    class _Choice:
        message = _Msg()

    class _Response:
        choices = [_Choice()]

    async def _fn(**kwargs: Any) -> Any:
        return _Response()

    extractor = LiteLLMEventExtractor(completion_fn=_fn)
    event = await extractor.extract(_article())
    assert event.event_type == "earnings"
