"""일일 브리핑 트리거 (Track D `briefing/` 모듈 호출).

v2 원본: `/report` POST (services/jobs/app.py:883-905) — DailyReporter.create_and_send_report.

v3 어댑터:
- 컨텐츠 생성은 Track D 의 `briefing.context_builder.collect_briefing_data` +
  `briefing.reporter.generate_briefing` 가 책임. 이 모듈은 두 함수의 호출 + Telegram
  발송 wiring 만 담당.
- LLM 호출자 (llm_caller) 는 옵션. 미주입 시 fallback HTML 사용 (Track D 가 보장).
- Telegram 은 TelegramBot.send_message 로 발송. dry_run/미설정 시 자동 skip.
"""

from __future__ import annotations

import logging
import os
from datetime import date

import httpx
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncEngine

from prime_jennie_runtime.briefing.reporter import LLMCaller, generate_briefing
from prime_jennie_runtime.infra.config import TelegramConfig
from prime_jennie_runtime.telegram_bot.bot import TelegramBot

logger = logging.getLogger(__name__)


async def _fetch_kis_balance(http: httpx.AsyncClient) -> list[dict] | None:
    """KIS gateway /api/balance 응답의 positions 리스트. 실패 시 None — context_builder
    가 DB fallback 으로 우회한다."""
    url = os.environ.get("KIS_GATEWAY_URL", "http://kis-gateway:8080")
    try:
        resp = await http.get(f"{url.rstrip('/')}/api/balance", timeout=5.0)
        resp.raise_for_status()
        positions = resp.json().get("positions") or []
        return list(positions)
    except Exception:
        logger.warning(
            "daily_briefing: KIS balance fetch failed — falling back to DB", exc_info=True
        )
        return None


async def daily_briefing_report(
    engine: AsyncEngine,
    http: httpx.AsyncClient,
    *,
    telegram_config: TelegramConfig | None,
    llm_caller: LLMCaller | None = None,
    as_of: date | None = None,
    redis_client: aioredis.Redis | None = None,
) -> dict:
    """v2 `/report` 포팅 — Track D briefing 호출.

    1) collect_briefing_data(engine, redis_client) → data dict
       redis_client 가 있으면 macro dict 에 VIX/닛케이/항셍/SOX/NVDA/환율/수급 병합
    2) generate_briefing(data, llm_caller=...) → BriefingResult
    3) telegram_config 있으면 TelegramBot.send_message (HTML)

    Returns: {"source": "llm"|"fallback", "html_len": N, "telegram_sent": bool}
    """
    # 지연 import: briefing.context_builder 가 없는 환경 (테스트) 에서 import 실패 방지.
    from prime_jennie_runtime.briefing.context_builder import collect_briefing_data

    kis_balance = await _fetch_kis_balance(http)
    data = await collect_briefing_data(
        engine, as_of=as_of, redis_client=redis_client, kis_balance=kis_balance
    )
    result = await generate_briefing(data, llm_caller=llm_caller)

    telegram_sent = False
    if telegram_config is not None:
        bot = TelegramBot(telegram_config, client=http)
        telegram_sent = await bot.send_message(result.html, parse_mode="HTML")

    logger.info(
        "daily_briefing_report: source=%s html_len=%d telegram_sent=%s",
        result.source,
        len(result.html),
        telegram_sent,
    )
    return {
        "source": result.source,
        "html_len": len(result.html),
        "telegram_sent": telegram_sent,
    }


__all__ = ["daily_briefing_report"]
