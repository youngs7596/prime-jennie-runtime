"""감성/메타데이터 추출 추상화.

2026-04-21 이후 운영은 ``EventExtractor`` 로 전환 — EXAONE 이 이벤트 분류 + 메타
데이터를 structured JSON 으로 추출. 과거 ``SentimentAnalyzer`` Protocol 은
테스트/호환용으로 일부 남기지만 파이프라인 기본 경로는 Event.

``Embedder`` 는 Qdrant 제거와 함께 사라짐.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from .models import (
    EventType,
    ImpactLevel,
    NewsArticle,
    NewsEvent,
    SentimentLabel,
    SentimentScore,
    TimeHorizon,
)

# ---------------------------------------------------------------------
# SentimentAnalyzer — legacy
# ---------------------------------------------------------------------


@runtime_checkable
class SentimentAnalyzer(Protocol):
    """단일 기사 → SentimentScore. legacy path."""

    async def analyze(self, article: NewsArticle) -> SentimentScore: ...


_DEFAULT_POSITIVE = (
    "상승",
    "호조",
    "신고가",
    "흑자",
    "수주",
    "급등",
    "어닝서프라이즈",
    "최대",
    "확대",
)
_DEFAULT_NEGATIVE = (
    "하락",
    "급락",
    "적자",
    "손실",
    "감소",
    "리콜",
    "악재",
    "하향",
    "둔화",
)


def _label_for(
    score: float, *, pos_threshold: float = 0.2, neg_threshold: float = -0.2
) -> SentimentLabel:
    if score >= pos_threshold:
        return "positive"
    if score <= neg_threshold:
        return "negative"
    return "neutral"


def _score_from_keywords(text: str, positive: tuple[str, ...], negative: tuple[str, ...]) -> float:
    pos = sum(1 for kw in positive if kw in text)
    neg = sum(1 for kw in negative if kw in text)
    denom = pos + neg + 1
    raw = (pos - neg) / denom
    return max(-1.0, min(1.0, raw))


@dataclass
class StubSentimentAnalyzer:
    """키워드 기반 결정론 감성. legacy 호환 테스트."""

    positive_keywords: tuple[str, ...] = _DEFAULT_POSITIVE
    negative_keywords: tuple[str, ...] = _DEFAULT_NEGATIVE
    model_name: str = "stub-keyword-v1"

    async def analyze(self, article: NewsArticle) -> SentimentScore:
        text = f"{article.title}\n{article.body}"
        score = _score_from_keywords(text, self.positive_keywords, self.negative_keywords)
        return SentimentScore(
            article_id=article.article_id,
            ticker=article.ticker,
            score=score,
            label=_label_for(score),
            analyzed_at=datetime.now(),
            model=self.model_name,
        )


# ---------------------------------------------------------------------
# EventExtractor — 신규 운영 경로
# ---------------------------------------------------------------------


@runtime_checkable
class EventExtractor(Protocol):
    """단일 기사 → NewsEvent. EXAONE structured JSON 추출."""

    async def extract(self, article: NewsArticle) -> NewsEvent: ...


# event_type 키워드 힌트 (Stub 전용) — 실 운영 LLM 은 이보다 훨씬 정교.
# 순서가 우선순위: 먼저 매칭되는 타입이 할당됨. 구체적·드문 타입을 위로.
_EVENT_TYPE_HINTS: tuple[tuple[EventType, tuple[str, ...]], ...] = (
    # 기업 이벤트 (구체 키워드 먼저)
    ("bankruptcy", ("상장폐지", "워크아웃", "회생", "파산")),
    ("shareholder_return", ("배당", "자사주", "밸류업", "주주환원")),
    ("investment", ("유상증자", "회사채", "자금조달", "투자유치")),
    ("mna", ("인수", "합병", "M&A", "매각", "인수합병", "지분")),
    ("lawsuit", ("소송", "고소", "제소", "손해배상", "패소", "승소")),
    ("earnings", ("실적", "매출", "영업이익", "순이익", "어닝", "분기", "흑자", "적자")),
    ("product", ("출시", "공개", "론칭", "신제품", "상용화")),
    ("personnel", ("CEO", "대표", "선임", "사임", "인사", "취임")),
    ("contract", ("수주", "계약", "체결", "공급")),
    ("strike", ("파업", "노조", "쟁의", "협상")),
    # 시장/거시
    ("geopolitical", ("이란", "호르무즈", "미중", "우크라이나", "제재", "관세", "전쟁")),
    ("regulation", ("규제", "승인", "허가", "심사", "정책")),
    ("market_movement", ("코스피", "코스닥", "지수", "폭락", "급등락", "거래량", "시장심리")),
    # 금융상품/분석
    ("fund_product", ("ETF", "펀드", "순자산", "운용사", "TDF")),
    ("analyst_rating", ("목표가", "매수의견", "매도의견", "리포트", "컨센서스")),
)


def _infer_event_type(text: str) -> EventType:
    for event_type, hints in _EVENT_TYPE_HINTS:
        if any(kw in text for kw in hints):
            return event_type
    return "other"


def _infer_impact(text: str, score: float) -> ImpactLevel:
    if abs(score) >= 0.5:
        return "high"
    if abs(score) >= 0.2:
        return "medium"
    return "low"


def _infer_time_horizon(event_type: EventType) -> TimeHorizon:
    if event_type in ("market_movement", "geopolitical", "strike"):
        return "immediate"
    if event_type in ("earnings", "mna", "lawsuit", "analyst_rating", "fund_product"):
        return "short"
    if event_type in ("regulation", "personnel", "shareholder_return", "investment"):
        return "medium"
    if event_type == "bankruptcy":
        return "long"
    return "short"


@dataclass
class StubEventExtractor:
    """결정론 키워드 기반 이벤트 추출기 — 테스트/개발 전용.

    운영에선 ``LiteLLMEventExtractor`` (EXAONE structured JSON) 이 대체.
    """

    positive_keywords: tuple[str, ...] = _DEFAULT_POSITIVE
    negative_keywords: tuple[str, ...] = _DEFAULT_NEGATIVE
    model_name: str = "stub-event-v1"

    async def extract(self, article: NewsArticle) -> NewsEvent:
        text = f"{article.title}\n{article.body}"
        score = _score_from_keywords(text, self.positive_keywords, self.negative_keywords)
        event_type = _infer_event_type(text)
        return NewsEvent(
            article_id=article.article_id,
            ticker=article.ticker,
            published_at=article.published_at,
            event_type=event_type,
            impact_level=_infer_impact(text, score),
            sentiment=_label_for(score),
            sentiment_score=score,
            time_horizon=_infer_time_horizon(event_type),
            keywords=[kw for kw in (self.positive_keywords + self.negative_keywords) if kw in text][
                :5
            ],
            sector_tags=[],
            financial_signals=[],
            confidence=0.5,
            model=self.model_name,
            analyzed_at=datetime.now(),
        )
