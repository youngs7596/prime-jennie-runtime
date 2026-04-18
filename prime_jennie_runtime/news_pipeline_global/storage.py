"""PostgreSQL upsert — articles + digests.

asyncpg pool 을 받아 작업. SQLAlchemy engine 대신 asyncpg 를 쓴 이유는 jobs/app.py
의 다른 v2-포팅 job 들과 스타일 통일 (asyncpg.Pool 을 공용 리소스로 공유).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import asyncpg

from .models import GlobalNewsArticle, MacroNewsDigest

logger = logging.getLogger(__name__)


async def upsert_articles(pool: asyncpg.Pool, articles: list[GlobalNewsArticle]) -> int:
    """신규 article_id 만 INSERT. 기존은 무시 (RSS 는 동일 URL 반복 노출됨)."""
    if not articles:
        return 0
    inserted = 0
    async with pool.acquire() as conn, conn.transaction():
        for a in articles:
            result = await conn.execute(
                "INSERT INTO global_macro_news_articles "
                "(article_id, source, feed_name, title, description, source_url, published_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7) "
                "ON CONFLICT (article_id) DO NOTHING",
                a.article_id,
                a.source,
                a.feed_name,
                a.title,
                a.description,
                a.source_url,
                a.published_at,
            )
            if result.endswith(" 1"):
                inserted += 1
    return inserted


async def fetch_recent_articles(
    pool: asyncpg.Pool, since: datetime, limit: int = 50
) -> list[GlobalNewsArticle]:
    """`since` 이후 published_at 인 기사를 최신순으로 가져옴."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT article_id, source, feed_name, title, description, source_url, "
            "published_at FROM global_macro_news_articles "
            "WHERE published_at >= $1 "
            "ORDER BY published_at DESC LIMIT $2",
            since,
            limit,
        )
    return [
        GlobalNewsArticle(
            article_id=r["article_id"],
            source=r["source"],
            feed_name=r["feed_name"],
            title=r["title"],
            description=r["description"],
            source_url=r["source_url"],
            published_at=r["published_at"],
        )
        for r in rows
    ]


async def upsert_digest(pool: asyncpg.Pool, digest: MacroNewsDigest) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO global_macro_news_digests "
            "(digest_date, digest_id, summary, headlines, raw_count, source_counts, "
            "summary_model, built_at) "
            "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, NOW()) "
            "ON CONFLICT (digest_date) DO UPDATE SET "
            "digest_id=EXCLUDED.digest_id, summary=EXCLUDED.summary, "
            "headlines=EXCLUDED.headlines, raw_count=EXCLUDED.raw_count, "
            "source_counts=EXCLUDED.source_counts, summary_model=EXCLUDED.summary_model, "
            "built_at=NOW()",
            digest.digest_date.date()
            if isinstance(digest.digest_date, datetime)
            else digest.digest_date,
            digest.digest_id,
            digest.summary,
            digest.headlines,
            digest.raw_count,
            json.dumps(digest.source_counts),
            digest.summary_model,
        )


async def fetch_latest_digest(
    pool: asyncpg.Pool, max_age_hours: int = 36
) -> MacroNewsDigest | None:
    """RealWsjDigestFeeder 가 사용. 36h 초과면 None (너무 오래된 digest 는 무효)."""
    cutoff = datetime.now(tz=UTC) - timedelta(hours=max_age_hours)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT digest_date, digest_id, summary, headlines, raw_count, "
            "source_counts, summary_model, built_at "
            "FROM global_macro_news_digests "
            "WHERE built_at >= $1 "
            "ORDER BY digest_date DESC LIMIT 1",
            cutoff,
        )
    if row is None:
        return None
    counts = row["source_counts"]
    if isinstance(counts, str):
        counts = json.loads(counts)
    return MacroNewsDigest(
        digest_date=row["digest_date"],
        digest_id=row["digest_id"],
        summary=row["summary"],
        headlines=row["headlines"],
        raw_count=row["raw_count"],
        source_counts=counts,
        summary_model=row["summary_model"],
    )
