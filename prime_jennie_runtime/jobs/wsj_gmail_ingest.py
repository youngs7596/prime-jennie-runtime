"""WSJ Newsletter 수집 + 한국어 요약 + Telegram 발송.

v2 원본: `prime_jennie/services/jobs/app.py::council_trigger` 내
`fetch_wsj_briefing` + `_summarize_and_send_wsj` 블록.

v3 에선 독립 job 으로 분리:
  1. Gmail API 로 4종 뉴스레터 fetch (blocking google-api-python-client →
     asyncio.to_thread 로 격리)
  2. `global_macro_news_articles` 에 `source='wsj_newsletter'` upsert —
     기존 07:30 `global_news_digest` 가 자동 흡수 → `RealWsjDigestFeeder` 경로로
     Macro Council 에 반영
  3. 각 newsletter 를 Claude Opus 로 한국어 요약 → Telegram 발송.
     Redis dedup key 로 하루 1회 보장 (`wsj:summary:sent:{type}:{date}`)

cron: `20 7 * * 1-5` (07:30 global_news_digest 직전). 08:30 첫 Macro run 에 반영.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime

import asyncpg
import httpx
import redis.asyncio as aioredis

from prime_jennie_runtime.briefing.reporter import LLMCaller
from prime_jennie_runtime.infra.config import TelegramConfig
from prime_jennie_runtime.news_pipeline_global.models import GlobalNewsArticle
from prime_jennie_runtime.news_pipeline_global.storage import upsert_articles
from prime_jennie_runtime.news_pipeline_global.wsj_gmail import (
    WSJNewsletter,
    fetch_wsj_briefing,
    type_to_label,
)
from prime_jennie_runtime.telegram_bot.bot import TelegramBot

logger = logging.getLogger(__name__)

WSJ_SOURCE = "wsj_newsletter"
WSJ_FEED_NAME = "WSJ Subscribed Newsletters"

WSJ_SUMMARY_SYSTEM = """당신은 한국 주식시장 투자자를 위한 매크로 뉴스 애널리스트입니다.
WSJ 뉴스레터 원문을 분석하여 KOSPI 투자자 관점에서 핵심 내용을 한국어로 요약하세요.

출력 형식 (반드시 준수):

<b>[뉴스레터 종류]</b> — YYYY.MM.DD

<b>핵심 요약</b>
- 주요 포인트를 불릿으로 3~5줄

<b>KOSPI 영향도</b>
- 관련 섹터/종목에 미칠 영향 1~2줄

<b>원문 키워드:</b> 영문 핵심 키워드 3~5개

지시사항:
- 한국어로 작성, HTML 태그만 사용 (<b>, <i> 허용, Markdown 금지)
- KOSPI 투자자 관점에서 중요한 내용 우선
- 광고/구독 권유 문구 완전 제거
- 전체 길이 1500자 이내"""


def _article_id(nl: WSJNewsletter) -> str:
    """뉴스레터당 안정 ID. subject + date 조합이 같으면 같은 메일."""
    seed = f"wsj|{nl.newsletter_type}|{nl.subject}|{nl.email_date}"
    return f"wsj_{hashlib.sha256(seed.encode()).hexdigest()[:28]}"


def _parse_email_date(raw: str) -> datetime:
    """RFC 2822 → UTC datetime. 실패 시 now()."""
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except Exception:
        return datetime.now(UTC)


def _to_article(nl: WSJNewsletter) -> GlobalNewsArticle:
    return GlobalNewsArticle(
        article_id=_article_id(nl),
        source=WSJ_SOURCE,
        feed_name=WSJ_FEED_NAME,
        title=f"{type_to_label(nl.newsletter_type)} — {nl.subject}",
        description=nl.body,
        source_url=f"gmail://{nl.newsletter_type}/{_article_id(nl)}",
        published_at=_parse_email_date(nl.email_date),
    )


async def _already_summarized(redis_client: aioredis.Redis, nl: WSJNewsletter) -> bool:
    key = f"wsj:summary:sent:{nl.newsletter_type}:{date.today().isoformat()}"
    return bool(await redis_client.exists(key))


async def _mark_summarized(redis_client: aioredis.Redis, nl: WSJNewsletter) -> None:
    key = f"wsj:summary:sent:{nl.newsletter_type}:{date.today().isoformat()}"
    # 36 시간 TTL — 다음날 분이 충분히 들어올 시간 + 자동 정리
    await redis_client.set(key, "1", ex=36 * 3600)


async def _summarize_and_send(
    nl: WSJNewsletter,
    *,
    llm_caller: LLMCaller,
    bot: TelegramBot,
    redis_client: aioredis.Redis,
) -> bool:
    if await _already_summarized(redis_client, nl):
        logger.debug("WSJ summary already sent today: %s", nl.newsletter_type)
        return False

    label = type_to_label(nl.newsletter_type)
    prompt = (
        f"뉴스레터 종류: {label}\n날짜: {nl.email_date}\n제목: {nl.subject}\n\n본문:\n{nl.body}"
    )
    try:
        summary = await llm_caller(WSJ_SUMMARY_SYSTEM, prompt)
    except Exception:
        logger.warning("WSJ summary LLM failed: %s", nl.newsletter_type, exc_info=True)
        return False

    if not summary:
        return False

    sent = await bot.send_message(summary.strip(), parse_mode="HTML")
    if sent:
        await _mark_summarized(redis_client, nl)
    return sent


async def wsj_gmail_ingest(
    pool: asyncpg.Pool,
    http: httpx.AsyncClient,
    redis_client: aioredis.Redis,
    *,
    credentials_path: str,
    token_path: str,
    telegram_config: TelegramConfig | None,
    llm_caller: LLMCaller | None = None,
) -> dict:
    """Gmail → global_macro_news_articles + Telegram 요약.

    Returns: {"fetched": N, "inserted": N, "summaries_sent": N}
    """
    briefing = await asyncio.to_thread(fetch_wsj_briefing, credentials_path, token_path, 10)
    newsletters = briefing.newsletters

    if not newsletters:
        logger.info("wsj_gmail_ingest: no newsletters")
        return {"fetched": 0, "inserted": 0, "summaries_sent": 0}

    articles = [_to_article(nl) for nl in newsletters]
    inserted = await upsert_articles(pool, articles)

    summaries_sent = 0
    if llm_caller is not None and telegram_config is not None:
        bot = TelegramBot(telegram_config, client=http)
        for nl in newsletters:
            if await _summarize_and_send(
                nl, llm_caller=llm_caller, bot=bot, redis_client=redis_client
            ):
                summaries_sent += 1
    else:
        logger.info(
            "wsj_gmail_ingest: telegram/llm 미주입 — 요약/발송 skip (llm=%s tg=%s)",
            llm_caller is not None,
            telegram_config is not None,
        )

    logger.info(
        "wsj_gmail_ingest: fetched=%d inserted=%d summaries_sent=%d",
        len(newsletters),
        inserted,
        summaries_sent,
    )
    return {
        "fetched": len(newsletters),
        "inserted": inserted,
        "summaries_sent": summaries_sent,
    }


__all__ = ["wsj_gmail_ingest"]
