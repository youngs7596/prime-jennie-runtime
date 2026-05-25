"""End-to-end 오케스트레이션 — crawl_cycle + build_digest."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx

from prime_jennie_runtime.dashboard.llm_pricing import calc_shadow_cost
from prime_jennie_runtime.infra.llm_stats import record_llm_call

from .feeds import RssFeed, load_feeds
from .models import MacroNewsDigest
from .rss_crawler import crawl_all
from .storage import fetch_recent_articles, upsert_articles, upsert_digest
from .summarizer import summarize

logger = logging.getLogger(__name__)


async def crawl_cycle(
    pool: asyncpg.Pool,
    http: httpx.AsyncClient,
    *,
    feeds: tuple[RssFeed, ...] | None = None,
) -> dict[str, int]:
    """RSS → Postgres (idempotent)."""
    feeds = feeds or load_feeds()
    articles = await crawl_all(feeds, client=http)
    inserted = await upsert_articles(pool, articles)
    by_source: dict[str, int] = {"_total": len(articles), "_inserted": inserted}
    for a in articles:
        by_source[a.source] = by_source.get(a.source, 0) + 1
    logger.info(
        "global_news.crawl_cycle done total=%d inserted=%d",
        len(articles),
        inserted,
    )
    return by_source


def _digest_id(headlines: str) -> str:
    return hashlib.sha256(headlines.encode("utf-8")).hexdigest()[:16]


def _format_headlines(articles: list) -> str:
    if not articles:
        return "- (no recent global macro headlines)"
    lines = []
    for a in articles[:20]:
        ts = a.published_at.strftime("%m-%d %H:%M") if a.published_at else "--"
        title = a.title.replace("\n", " ")[:160]
        lines.append(f"- [{a.source}] {ts} {title}")
    return "\n".join(lines)


async def build_digest(
    pool: asyncpg.Pool,
    http: httpx.AsyncClient,
    *,
    as_of: datetime | None = None,
    lookback_hours: int = 24,
    summary_api_key: str | None = None,
    summary_base_url: str | None = None,
    summary_model: str | None = None,
    redis_client: Any = None,
) -> MacroNewsDigest:
    now = as_of or datetime.now(tz=UTC)
    since = now - timedelta(hours=lookback_hours)
    articles = await fetch_recent_articles(pool, since, limit=50)
    headlines = _format_headlines(articles)
    summary, model_used, usage = await summarize(
        articles,
        api_key=summary_api_key,
        base_url=summary_base_url,
        model=summary_model,
        http_client=http,
    )
    counts: dict[str, int] = {}
    for a in articles:
        counts[a.source] = counts.get(a.source, 0) + 1
    digest = MacroNewsDigest(
        digest_date=now,
        digest_id=_digest_id(headlines),
        summary=summary,
        headlines=headlines,
        raw_count=len(articles),
        source_counts=counts,
        summary_model=model_used,
    )
    await upsert_digest(pool, digest)
    # LLM 호출이 실제로 일어난 경우만 stats 누적 (fallback 경로는 model_used=None).
    # DeepSeek chat 가격은 dashboard.llm_pricing.PRICING 의 단일 source 사용.
    if model_used and usage is not None and redis_client is not None:
        cost = calc_shadow_cost(usage["input_tokens"], usage["output_tokens"])
        await record_llm_call(
            redis_client,
            service="news_global_digest",
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cost_usd=cost,
            timestamp=now,
        )
    logger.info(
        "global_news.build_digest done count=%d summary_model=%s",
        len(articles),
        model_used,
    )
    return digest
