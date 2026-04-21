"""Track E — News Pipeline KOR.

v2 ``prime_jennie/services/news`` 포팅. 핵심 산출물:
- 3-stage Redis Stream 소비 ``NewsPipeline`` (collect_and_publish / analyze_stream_once /
  archive_stream_once). app.py 가 3 async loop 로 상시 구동.
- Scout 공급 ``NewsPipelineScoutFeeder`` (Track B ``NewsScoreFeeder`` Protocol 구현)

외부 의존(Naver 크롤링, EXAONE LLM, kure-v1 임베딩, Qdrant)은 Protocol + Stub로
Phase 1에서 추상화. 실제 어댑터는 Phase 2에서 v2 코드 그대로 포팅.
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
    NewsArticle,
    NewsEmbedding,
    NewsScoreSnapshot,
    SentimentLabel,
    SentimentScore,
)
from .pipeline import CycleStats, NewsPipeline
from .scout_feeder import NewsPipelineScoutFeeder
from .sentiment import (
    Embedder,
    SentimentAnalyzer,
    StubEmbedder,
    StubSentimentAnalyzer,
)
from .storage import (
    InMemorySentimentRepo,
    InMemoryVectorStore,
    SentimentRepo,
    VectorStore,
    lookback_since,
)

__all__ = [
    "DEDUP_TTL_SECONDS",
    "DEDUP_WINDOW_DAYS",
    "CycleStats",
    "Embedder",
    "InMemoryDeduplicator",
    "InMemorySentimentRepo",
    "InMemoryVectorStore",
    "NewsArticle",
    "NewsCrawler",
    "NewsDeduplicator",
    "NewsEmbedding",
    "NewsPipeline",
    "NewsPipelineScoutFeeder",
    "NewsScoreSnapshot",
    "RedisDeduplicator",
    "SentimentAnalyzer",
    "SentimentLabel",
    "SentimentRepo",
    "SentimentScore",
    "StubCrawler",
    "StubEmbedder",
    "StubSentimentAnalyzer",
    "VectorStore",
    "article_fingerprint",
    "lookback_since",
    "make_dummy_article",
]
