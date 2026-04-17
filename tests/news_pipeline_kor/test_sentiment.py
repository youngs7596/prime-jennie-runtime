"""키워드 기반 stub sentiment + stub embedder 결정성."""

from __future__ import annotations

from datetime import datetime

import pytest

from prime_jennie_runtime.news_pipeline_kor.crawler import make_dummy_article
from prime_jennie_runtime.news_pipeline_kor.sentiment import (
    StubEmbedder,
    StubSentimentAnalyzer,
)


@pytest.mark.asyncio
async def test_positive_keywords_yield_positive_label():
    a = make_dummy_article("005930", title="삼성전자 신고가 경신, 어닝서프라이즈")
    s = await StubSentimentAnalyzer().analyze(a)
    assert s.label == "positive"
    assert s.score > 0


@pytest.mark.asyncio
async def test_negative_keywords_yield_negative_label():
    a = make_dummy_article("005930", title="삼성전자 급락 적자 손실")
    s = await StubSentimentAnalyzer().analyze(a)
    assert s.label == "negative"
    assert s.score < 0


@pytest.mark.asyncio
async def test_no_keywords_neutral():
    a = make_dummy_article("005930", title="삼성전자 분기 실적 발표")
    s = await StubSentimentAnalyzer().analyze(a)
    assert s.label == "neutral"
    assert s.score == 0.0


@pytest.mark.asyncio
async def test_score_bounded_minus1_plus1():
    a = make_dummy_article(
        "005930", title="상승 호조 신고가 흑자 수주 급등 어닝서프라이즈 최대 확대 상승"
    )
    s = await StubSentimentAnalyzer().analyze(a)
    assert -1.0 <= s.score <= 1.0


@pytest.mark.asyncio
async def test_sentiment_carries_article_meta():
    pub = datetime(2026, 4, 17, 9, 0, 0)
    a = make_dummy_article("000660", title="SK하이닉스 호조", published_at=pub)
    s = await StubSentimentAnalyzer().analyze(a)
    assert s.ticker == "000660"
    assert s.article_id == a.article_id
    assert s.model == "stub-keyword-v1"


# ---------- Embedder ----------


@pytest.mark.asyncio
async def test_embedder_deterministic_same_article():
    e = StubEmbedder(dimension=8)
    a = make_dummy_article("005930", title="x")
    v1 = await e.embed(a)
    v2 = await e.embed(a)
    assert v1 == v2
    assert len(v1) == 8


@pytest.mark.asyncio
async def test_embedder_distinct_articles_distinct_vectors():
    e = StubEmbedder(dimension=8)
    a = make_dummy_article("005930", title="A")
    b = make_dummy_article("005930", title="B")
    v_a = await e.embed(a)
    v_b = await e.embed(b)
    assert v_a != v_b
