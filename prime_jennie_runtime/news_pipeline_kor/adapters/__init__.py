"""외부 서비스 어댑터 — Naver 크롤러 + EXAONE + Postgres.

Qdrant/kure-v1 어댑터는 2026-04-21 제거 (metadata 추출 전환).
sentiment 어댑터는 event 추출기로 대체.
"""

from .exaone_extractor import LiteLLMEventExtractor
from .naver_crawler import NAVER_NOISE_KEYWORDS, NaverNewsCrawler
from .pg_event_repo import PostgresEventRepo
from .pg_sentiment_repo import PostgresSentimentRepo

__all__ = [
    "NAVER_NOISE_KEYWORDS",
    "LiteLLMEventExtractor",
    "NaverNewsCrawler",
    "PostgresEventRepo",
    "PostgresSentimentRepo",
]
