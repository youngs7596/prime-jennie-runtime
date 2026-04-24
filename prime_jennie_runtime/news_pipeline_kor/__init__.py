"""Track E — News Pipeline KOR.

v2 ``prime_jennie/services/news`` 포팅. 핵심 산출물:
- 2-stage Redis Stream 소비 ``NewsPipeline`` (collect_and_publish / extract_stream_once).
  app.py 가 2 async loop 로 상시 구동.
- Scout 공급 ``NewsPipelineScoutFeeder`` (Track B ``NewsEventFeeder`` Protocol 구현).

2026-04-21: sentiment → metadata 추출 전환. Qdrant/kure-v1 archiver 제거.
"""

from .crawler import NewsCrawler, StubCrawler, make_dummy_article
from .dedup import (
    DEDUP_TTL_SECONDS,
    DEDUP_WINDOW_DAYS,
    InMemoryDeduplicator,
    NewsDeduplicator,
    RedisDeduplicator,
    article_fingerprint,
)
from .models import (
    EventType,
    FinancialSignal,
    ImpactLevel,
    NewsArticle,
    NewsEvent,
    SentimentLabel,
    SentimentScore,
    SignalDirection,
    TimeHorizon,
)
from .pipeline import CycleStats, NewsPipeline
from .scout_feeder import NewsPipelineScoutFeeder
from .sentiment import (
    EventExtractor,
    SentimentAnalyzer,
    StubEventExtractor,
    StubSentimentAnalyzer,
)
from .storage import (
    EventRepo,
    InMemoryEventRepo,
    InMemorySentimentRepo,
    SentimentRepo,
    lookback_since,
)

__all__ = [
    "DEDUP_TTL_SECONDS",
    "DEDUP_WINDOW_DAYS",
    "CycleStats",
    "EventExtractor",
    "EventRepo",
    "EventType",
    "FinancialSignal",
    "ImpactLevel",
    "InMemoryDeduplicator",
    "InMemoryEventRepo",
    "InMemorySentimentRepo",
    "NewsArticle",
    "NewsCrawler",
    "NewsDeduplicator",
    "NewsEvent",
    "NewsPipeline",
    "NewsPipelineScoutFeeder",
    "RedisDeduplicator",
    "SentimentAnalyzer",
    "SentimentLabel",
    "SentimentRepo",
    "SentimentScore",
    "SignalDirection",
    "StubCrawler",
    "StubEventExtractor",
    "StubSentimentAnalyzer",
    "TimeHorizon",
    "article_fingerprint",
    "lookback_since",
    "make_dummy_article",
]
