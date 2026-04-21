"""News Pipeline Redis Stream 상수 및 직렬화 헬퍼.

v2 ``prime_jennie/services/news/collector.py`` 의 ``stream:news:raw`` 패턴을 v3 로 포팅.
Stream 이름은 ``v3:`` prefix 로 분리 — 같은 Redis 를 v2 공존 기간에 공유하던 시절의
잔여 메시지/consumer group 과 혼선되지 않도록.

흐름:
    Collector → XADD v3:news:raw
             → Analyzer  (XREADGROUP v3:news:analyzer → LLM → PG → XACK)
             → Archiver  (XREADGROUP v3:news:archiver → embed → Qdrant → XACK)
"""

from __future__ import annotations

from .models import NewsArticle

# Stream / group names (v2 와 충돌 회피를 위해 v3: prefix)
NEWS_STREAM = "v3:news:raw"
ANALYZER_GROUP = "v3:news:analyzer"
ARCHIVER_GROUP = "v3:news:archiver"
ANALYZER_CONSUMER = "analyzer_1"
ARCHIVER_CONSUMER = "archiver_1"

# Stream 크기 상한 — maxlen approx. v2 동일.
NEWS_STREAM_MAXLEN = 10_000

# XREADGROUP BLOCK 시간 (ms). 메시지 없을 때 대기. 너무 길면 종료 응답 지연,
# 너무 짧으면 idle CPU. v2 와 동일하게 2 초.
BLOCK_MS = 2_000

# 단일 XREADGROUP 당 batch 크기. v2 는 analyzer 20 / archiver 100 이었으나
# v3 는 burst 를 더 얇게 분산하려 analyzer 10. LLM 동시 호출 수가 그대로 batch
# 라 GPU peak 도 절반으로 내려간다. 임베딩은 가벼우므로 20 유지.
ANALYZER_BATCH = 10
ARCHIVER_BATCH = 20


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
