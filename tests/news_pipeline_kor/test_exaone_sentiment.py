"""LiteLLMSentimentAnalyzer — completion_fn 주입으로 LiteLLM 없이 단위 검증."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from prime_jennie_runtime.news_pipeline_kor.adapters.exaone_sentiment import (
    LiteLLMSentimentAnalyzer,
    _parse_score,
    _to_v3_score,
)
from prime_jennie_runtime.news_pipeline_kor.models import NewsArticle
from prime_jennie_runtime.news_pipeline_kor.sentiment import SentimentAnalyzer


def _article(title: str = "삼성전자 호실적", body: str = "") -> NewsArticle:
    return NewsArticle(
        article_id="a1",
        ticker="005930",
        title=title,
        body=body,
        published_at=datetime.now(),
        source_url="https://example.com/1",
        source_name="연합",
    )


def _fake_completion(content: str):
    async def _fn(**kwargs: Any) -> dict:
        return {"choices": [{"message": {"content": content}}]}

    return _fn


# ---------- _parse_score ----------


def test_parse_score_plain_json():
    assert _parse_score('{"score": 72, "reason": "호실적"}') == 72


def test_parse_score_clamps_out_of_range():
    assert _parse_score('{"score": 200}') == 100
    assert _parse_score('{"score": -50}') == 0


def test_parse_score_in_code_fence():
    raw = '```json\n{"score": 30, "reason": "하락"}\n```'
    assert _parse_score(raw) == 30


def test_parse_score_with_surrounding_text():
    raw = 'Here is the result: {"score": 55, "reason": "중립"} end.'
    assert _parse_score(raw) == 55


def test_parse_score_garbage_returns_none():
    assert _parse_score("not json at all") is None
    assert _parse_score("") is None


def test_parse_score_missing_field_returns_none():
    assert _parse_score('{"reason": "only reason"}') is None


# ---------- _to_v3_score ----------


def test_v2_to_v3_mapping():
    assert _to_v3_score(0) == -1.0
    assert _to_v3_score(50) == 0.0
    assert _to_v3_score(100) == 1.0
    assert _to_v3_score(75) == 0.5
    assert _to_v3_score(25) == -0.5


# ---------- analyzer: happy path ----------


@pytest.mark.asyncio
async def test_analyze_positive_response():
    analyzer = LiteLLMSentimentAnalyzer(
        completion_fn=_fake_completion('{"score": 80, "reason": "호실적 발표"}')
    )
    result = await analyzer.analyze(_article())
    assert result.score == pytest.approx(0.6)  # (80 - 50) / 50
    assert result.label == "positive"
    assert result.article_id == "a1"
    assert result.ticker == "005930"


@pytest.mark.asyncio
async def test_analyze_negative_response():
    analyzer = LiteLLMSentimentAnalyzer(
        completion_fn=_fake_completion('{"score": 20, "reason": "적자 전환"}')
    )
    result = await analyzer.analyze(_article(title="삼성전자 적자 전환"))
    assert result.score == pytest.approx(-0.6)
    assert result.label == "negative"


@pytest.mark.asyncio
async def test_analyze_neutral_response():
    analyzer = LiteLLMSentimentAnalyzer(
        completion_fn=_fake_completion('{"score": 55, "reason": "혼조"}')
    )
    result = await analyzer.analyze(_article())
    assert result.label == "neutral"


# ---------- analyzer: 실패 fallback ----------


@pytest.mark.asyncio
async def test_analyze_unparseable_response_returns_neutral():
    analyzer = LiteLLMSentimentAnalyzer(completion_fn=_fake_completion("I cannot analyze this."))
    result = await analyzer.analyze(_article())
    assert result.score == 0.0
    assert result.label == "neutral"


@pytest.mark.asyncio
async def test_analyze_completion_raises_returns_neutral():
    async def _boom(**kwargs: Any) -> dict:
        raise RuntimeError("rate limit")

    analyzer = LiteLLMSentimentAnalyzer(completion_fn=_boom)
    result = await analyzer.analyze(_article())
    assert result.score == 0.0
    assert result.label == "neutral"


@pytest.mark.asyncio
async def test_analyze_passes_model_to_completion_fn():
    captured: dict[str, Any] = {}

    async def _capture(**kwargs: Any) -> dict:
        captured.update(kwargs)
        return {"choices": [{"message": {"content": '{"score": 50}'}}]}

    analyzer = LiteLLMSentimentAnalyzer(
        model="ollama/exaone3.5:32b",
        api_base="http://localhost:11434",
        completion_fn=_capture,
    )
    await analyzer.analyze(_article(title="t", body="b"))
    assert captured["model"] == "ollama/exaone3.5:32b"
    assert captured["api_base"] == "http://localhost:11434"
    # prompt 가 한국어 프롬프트를 포함
    messages = captured["messages"]
    assert "감성을 분석" in messages[0]["content"]


@pytest.mark.asyncio
async def test_analyze_body_excerpt_truncation():
    captured: dict[str, Any] = {}

    async def _capture(**kwargs: Any) -> dict:
        captured.update(kwargs)
        return {"choices": [{"message": {"content": '{"score": 50}'}}]}

    long_body = "가" * 500
    analyzer = LiteLLMSentimentAnalyzer(completion_fn=_capture, body_excerpt_chars=100)
    await analyzer.analyze(_article(body=long_body))
    prompt = captured["messages"][0]["content"]
    # 본문 100자만 들어감 (300 전체가 아님)
    assert "가" * 100 in prompt
    assert "가" * 200 not in prompt


@pytest.mark.asyncio
async def test_analyzer_conforms_to_protocol():
    analyzer = LiteLLMSentimentAnalyzer(completion_fn=_fake_completion('{"score": 50}'))
    assert isinstance(analyzer, SentimentAnalyzer)


@pytest.mark.asyncio
async def test_analyze_handles_object_style_response():
    """LiteLLM 은 ModelResponse 객체(attribute access) 를 돌려줄 수도 있다."""

    class _Msg:
        content = '{"score": 70, "reason": "ok"}'

    class _Choice:
        message = _Msg()

    class _Response:
        choices = [_Choice()]

    async def _fn(**kwargs: Any) -> Any:
        return _Response()

    analyzer = LiteLLMSentimentAnalyzer(completion_fn=_fn)
    result = await analyzer.analyze(_article())
    assert result.score == pytest.approx(0.4)
