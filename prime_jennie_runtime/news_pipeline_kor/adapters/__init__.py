"""외부 서비스 어댑터 (Phase 2 포팅).

Phase 1 Stub 자리에 실 구현을 드롭인. 각 어댑터는 `news_pipeline_kor` 의
Protocol 을 구현.
"""

from .exaone_sentiment import LiteLLMSentimentAnalyzer
from .kure_embedder import KureEmbedder
from .naver_crawler import NAVER_NOISE_KEYWORDS, NaverNewsCrawler
from .pg_sentiment_repo import PostgresSentimentRepo
from .qdrant_store import QdrantVectorStore

__all__ = [
    "NAVER_NOISE_KEYWORDS",
    "KureEmbedder",
    "LiteLLMSentimentAnalyzer",
    "NaverNewsCrawler",
    "PostgresSentimentRepo",
    "QdrantVectorStore",
]
