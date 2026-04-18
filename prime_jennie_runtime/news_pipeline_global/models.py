"""Domain 모델 (pipeline 내부 교환용). DB row 도 아니고 wire 도 아닌 중간 표현."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class GlobalNewsArticle:
    article_id: str  # SHA-256(source_url)[:32]
    source: str  # wsj | bloomberg | reuters | google_news
    feed_name: str
    title: str
    description: str
    source_url: str
    published_at: datetime


@dataclass(frozen=True)
class MacroNewsDigest:
    digest_date: datetime  # date() 호출해서 DATE 로 저장
    digest_id: str
    summary: str
    headlines: str
    raw_count: int
    source_counts: dict[str, int]
    summary_model: str | None
