"""딴지 증시 요약 수집 — global_macro_news_articles 적재 (WSJ 수집의 형제).

설계: `.ai/designs/2026-06-26-ddanzi-market-summary-ingest.md`.

미국/한국 요약을 크롤해 `source='ddanzi_kimkr'` 로 upsert 한다. 기존 07:30
`global_news_digest`(24h lookback)가 source 무관하게 흡수 → `global_macro_news_digests`
→ `RealMacroNewsDigestFeeder`(36h) → 매크로 카운슬. 새 테이블·새 피더·마이그레이션이 없다.

정체성은 하드 입력이 아니라 맥락·검증 거울이다(설계 §정체성). 결측 허용 — 글이 없거나
크롤 실패면 조용히 skip 한다(매크로 피더가 알아서 강등). 자체 LLM 추출·텔레그램 발송은
비스코프(원문을 사람이 읽고, digest 가 요약).

published_at 은 수집 시각(now)으로 둔다. 글 페이지 메타가 시각만 주고 날짜를 안 줘서
제목 날짜로 잡으면 게시일과 하루 어긋날 수 있는데(미국 요약은 대상일 다음날 아침 게시),
잡이 글 올라온 직후 돌므로 수집 시각이 게시 시각에 가깝고 24h digest 창에 확실히 든다.
멱등은 article_id=hash(url) 라 재실행해도 ON CONFLICT DO NOTHING.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from prime_jennie_runtime.news_pipeline_global.models import GlobalNewsArticle
from prime_jennie_runtime.news_pipeline_global.storage import upsert_articles

from .crawlers.ddanzi import fetch_latest_summary

logger = logging.getLogger(__name__)

DDANZI_SOURCE = "ddanzi_kimkr"


def _article_id(url: str) -> str:
    return "ddanzi_" + hashlib.sha256(url.encode()).hexdigest()[:24]


async def ddanzi_ingest(
    pool: Any, http: httpx.AsyncClient, *, market: str = "US"
) -> dict[str, Any]:
    """딴지 최신 증시 요약을 global_macro_news_articles 에 upsert. 결측 허용."""
    summary = await fetch_latest_summary(http, market)
    if summary is None:
        logger.info("ddanzi_ingest(%s): no summary found — skip", market)
        return {"found": False, "upserted": 0}

    article = GlobalNewsArticle(
        article_id=_article_id(summary.url),
        source=DDANZI_SOURCE,
        feed_name=f"ddanzi_{market.lower()}_market_summary",
        title=summary.title,
        description=summary.body,
        source_url=summary.url,
        published_at=datetime.now(UTC),
    )
    inserted = await upsert_articles(pool, [article])
    logger.info(
        "ddanzi_ingest(%s): srl=%s title=%r upserted=%d",
        market,
        summary.document_srl,
        summary.title,
        inserted,
    )
    return {"found": True, "upserted": inserted, "document_srl": summary.document_srl}


__all__ = ["DDANZI_SOURCE", "ddanzi_ingest"]
