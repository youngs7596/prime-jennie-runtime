"""news_event_eval grading / aggregate logic 단위 테스트.

fake completion_fn 으로 LLM 호출을 대체해 offline 회귀 테스트.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from prime_jennie_runtime.news_pipeline_kor.adapters.exaone_extractor import (
    LiteLLMEventExtractor,
)
from prime_jennie_runtime.news_pipeline_kor.models import NewsArticle
from scripts.news_event_eval import (
    _aggregate,
    _grade,
    _run_one,
    _score_sign,
)


def _article(ticker: str = "005930") -> NewsArticle:
    return NewsArticle(
        article_id="a1",
        ticker=ticker,
        title="제목",
        body="본문",
        published_at=datetime(2026, 4, 21, 9, 0, 0),
        source_url="https://example.com/1",
        source_name="연합",
    )


def _completion(payload: dict[str, Any]):
    async def _fn(**_: Any) -> dict[str, Any]:
        return {"choices": [{"message": {"content": json.dumps(payload)}}]}

    return _fn


# ---------- _score_sign ----------


def test_score_sign_thresholds():
    assert _score_sign(0.2) == "positive"
    assert _score_sign(-0.2) == "negative"
    assert _score_sign(0.05) == "neutral"
    assert _score_sign(-0.1) == "neutral"


# ---------- _grade ----------


def test_grade_exact_match():
    expected = {
        "event_type": "earnings",
        "impact_level": "high",
        "sentiment": "positive",
        "sentiment_score_sign": "positive",
        "keywords_must_include": ["삼성전자", "HBM"],
        "sector_tags_must_include": ["반도체"],
    }

    from prime_jennie_runtime.news_pipeline_kor.models import NewsEvent

    event = NewsEvent(
        article_id="a1",
        ticker="005930",
        published_at=datetime(2026, 4, 21, 9, 0, 0),
        event_type="earnings",
        impact_level="high",
        sentiment="positive",
        sentiment_score=0.8,
        time_horizon="short",
        keywords=["삼성전자", "HBM", "어닝서프라이즈"],
        sector_tags=["반도체"],
        financial_signals=[],
        confidence=0.9,
        analyzed_at=datetime(2026, 4, 21, 9, 0, 0),
    )

    hits = _grade(expected, event)
    assert hits["event_type_match"] is True
    assert hits["impact_match"] is True
    assert hits["sentiment_match"] is True
    assert hits["sentiment_sign_match"] is True
    assert hits["keyword_hit_rate"] == 1.0
    assert hits["sector_hit_rate"] == 1.0


def test_grade_partial_keyword_hit():
    expected = {
        "event_type": "earnings",
        "impact_level": "high",
        "keywords_must_include": ["삼성전자", "HBM", "없는키워드"],
    }

    from prime_jennie_runtime.news_pipeline_kor.models import NewsEvent

    event = NewsEvent(
        article_id="a1",
        ticker="005930",
        published_at=datetime(2026, 4, 21, 9, 0, 0),
        event_type="earnings",
        impact_level="high",
        sentiment="positive",
        sentiment_score=0.8,
        time_horizon="short",
        keywords=["삼성전자", "HBM"],
        sector_tags=[],
        financial_signals=[],
        confidence=0.9,
        analyzed_at=datetime(2026, 4, 21, 9, 0, 0),
    )

    hits = _grade(expected, event)
    # 2 / 3 매칭
    assert hits["keyword_hit_rate"] == pytest.approx(2 / 3)


def test_grade_keyword_case_insensitive():
    expected = {
        "event_type": "earnings",
        "impact_level": "high",
        "keywords_must_include": ["HBM"],
    }

    from prime_jennie_runtime.news_pipeline_kor.models import NewsEvent

    event = NewsEvent(
        article_id="a1",
        ticker="005930",
        published_at=datetime(2026, 4, 21, 9, 0, 0),
        event_type="earnings",
        impact_level="high",
        sentiment="positive",
        sentiment_score=0.8,
        time_horizon="short",
        keywords=["hbm"],  # 소문자
        sector_tags=[],
        financial_signals=[],
        confidence=0.9,
        analyzed_at=datetime(2026, 4, 21, 9, 0, 0),
    )

    hits = _grade(expected, event)
    assert hits["keyword_hit_rate"] == 1.0


# ---------- _run_one ----------


@pytest.mark.asyncio
async def test_run_one_full_pipeline():
    fixture = {
        "article_id": "eval-test-01",
        "ticker": "005930",
        "title": "삼성전자 어닝서프라이즈",
        "body": "1분기 영업이익 7조원",
        "published_at": "2026-04-21T09:00:00+09:00",
        "source_url": "https://example.com/1",
        "expected": {
            "event_type": "earnings",
            "impact_level": "high",
            "sentiment": "positive",
            "keywords_must_include": ["삼성전자"],
        },
    }

    extractor = LiteLLMEventExtractor(
        model="dummy/model",
        completion_fn=_completion(
            {
                "event_type": "earnings",
                "impact_level": "high",
                "sentiment": "positive",
                "sentiment_score": 0.8,
                "time_horizon": "short",
                "keywords": ["삼성전자", "영업이익"],
                "sector_tags": ["반도체"],
                "financial_signals": [],
                "confidence": 0.9,
            }
        ),
    )

    result = await _run_one(extractor, fixture)
    assert result.article_id == "eval-test-01"
    assert result.hits["event_type_match"] is True
    assert result.hits["impact_match"] is True
    assert result.hits["sentiment_match"] is True
    assert result.hits["keyword_hit_rate"] == 1.0


@pytest.mark.asyncio
async def test_run_one_marks_extractor_exception():
    fixture = {
        "article_id": "eval-test-02",
        "ticker": "005930",
        "title": "t",
        "body": "",
        "published_at": "2026-04-21T09:00:00+09:00",
        "source_url": "https://example.com/2",
        "expected": {"event_type": "earnings"},
    }

    async def _bang(**_: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    # extract() 안에서는 try/except 로 삼키고 fallback 'other' 반환한다.
    # 따라서 _run_one 은 정상 CaseResult 를 반환하되 event_type_match=False 여야 한다.
    extractor = LiteLLMEventExtractor(model="dummy/model", completion_fn=_bang)
    result = await _run_one(extractor, fixture)
    assert result.actual.get("event_type") == "other"
    assert result.hits["event_type_match"] is False


# ---------- _aggregate ----------


def test_aggregate_computes_rates():
    from scripts.news_event_eval import CaseResult

    results = [
        CaseResult(
            article_id="a",
            title="t",
            expected={},
            actual={"event_type": "earnings"},
            hits={"event_type_match": True, "impact_match": True, "keyword_hit_rate": 1.0},
        ),
        CaseResult(
            article_id="b",
            title="t",
            expected={},
            actual={"event_type": "other"},
            hits={"event_type_match": False, "impact_match": True, "keyword_hit_rate": 0.5},
        ),
    ]
    agg = _aggregate(results)
    assert agg["n"] == 2
    assert agg["event_type_acc"] == 0.5
    assert agg["impact_acc"] == 1.0
    assert agg["keyword_hit_rate"] == pytest.approx(0.75)
    assert agg["schema_valid_rate"] == 1.0  # 둘 다 __extract_failed__ 아님


def test_aggregate_empty():
    agg = _aggregate([])
    assert agg["n"] == 0
    assert agg["event_type_acc"] == 0.0
    assert agg["keyword_hit_rate"] is None


def test_baseline_fixture_loads():
    """Baseline fixture 가 파싱 가능 + 각 케이스에 expected.event_type 존재."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent
    path = root / "tests" / "fixtures" / "news_eval" / "baseline.json"
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, list)
    assert len(data) >= 15
    for item in data:
        assert "article_id" in item
        assert "ticker" in item
        assert "title" in item
        assert "published_at" in item
        assert "expected" in item
        assert "event_type" in item["expected"]
