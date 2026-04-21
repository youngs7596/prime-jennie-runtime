"""News Pipeline Redis Stream 상수 및 직렬화 헬퍼.

v2 ``prime_jennie/services/news/collector.py`` 의 ``stream:news:raw`` 패턴을 v3 로 포팅.
Stream 이름은 ``v3:`` prefix 로 분리.

2026-04-21 메타데이터 전환: archiver/Qdrant 소비자 제거 → 단일 consumer (extractor).

흐름:
    Collector  → XADD v3:news:raw
    Extractor  → XREADGROUP v3:news:extractor → EXAONE metadata → PG news_events → XACK
"""

from __future__ import annotations

from .models import NewsArticle

# Stream / group names.
NEWS_STREAM = "v3:news:raw"
EXTRACTOR_GROUP = "v3:news:extractor"
EXTRACTOR_CONSUMER = "extractor_1"

# Stream 크기 상한 — maxlen approx. v2 동일.
NEWS_STREAM_MAXLEN = 10_000

# XREADGROUP BLOCK (ms). v2 동일 2 초.
BLOCK_MS = 2_000

# 단일 XREADGROUP 배치. EXAONE 메타데이터 추출은 감성 분석보다 응답 무거움 — 배치를
# 작게 잡아 GPU peak 을 평탄화.
EXTRACTOR_BATCH = 10


def serialize_article(article: NewsArticle) -> dict[str, str]:
    """Stream payload 로 쓸 flat str dict. Redis Streams 는 bytes/str 만 받는다."""
    return {"article_json": article.model_dump_json()}


def deserialize_article(data: dict) -> NewsArticle:
    """Stream payload → NewsArticle. ``xreadgroup`` 이 bytes 로 내주는 경우도 포함."""
    raw = data.get("article_json") or data.get(b"article_json")
    if isinstance(raw, bytes):
        raw = raw.decode()
    if not raw:
        raise ValueError(f"stream payload missing article_json: {data!r}")
    return NewsArticle.model_validate_json(raw)
