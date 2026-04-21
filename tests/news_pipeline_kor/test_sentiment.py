"""Stub sentiment + event extractor 결정성."""

from __future__ import annotations

from datetime import datetime

import pytest

from prime_jennie_runtime.news_pipeline_kor.crawler import make_dummy_article
from prime_jennie_runtime.news_pipeline_kor.sentiment import (
    StubEventExtractor,
    StubSentimentAnalyzer,
)

# ---------- Legacy SentimentAnalyzer (호환 유지) ----------


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


# ---------- EventExtractor (신규 주경로) ----------


@pytest.mark.asyncio
async def test_event_extractor_classifies_earnings_with_positive_sentiment():
    a = make_dummy_article("005930", title="삼성전자 분기 매출 영업이익 어닝서프라이즈 호조")
    event = await StubEventExtractor().extract(a)
    assert event.event_type == "earnings"
    assert event.sentiment == "positive"
    assert event.sentiment_score > 0
    assert event.ticker == "005930"
    assert event.article_id == a.article_id


@pytest.mark.asyncio
async def test_event_extractor_classifies_mna():
    a = make_dummy_article("005380", title="현대차 M&A 인수 지분 확대 호조")
    event = await StubEventExtractor().extract(a)
    assert event.event_type == "mna"


@pytest.mark.asyncio
async def test_event_extractor_unknown_falls_back_to_other():
    a = make_dummy_article("000660", title="그냥 평범한 뉴스 내용")
    event = await StubEventExtractor().extract(a)
    assert event.event_type == "other"
    assert event.impact_level == "low"
    assert event.sentiment == "neutral"


@pytest.mark.asyncio
async def test_event_extractor_preserves_published_at():
    pub = datetime(2026, 4, 21, 9, 0, 0)
    a = make_dummy_article("005930", title="삼성전자 매출 급등", published_at=pub)
    event = await StubEventExtractor().extract(a)
    assert event.published_at == pub
