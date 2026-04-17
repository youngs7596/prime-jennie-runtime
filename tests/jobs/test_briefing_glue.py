"""`daily_briefing_report` 스모크 — Track D briefing 호출 + Telegram wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from prime_jennie_runtime.briefing.reporter import BriefingResult
from prime_jennie_runtime.infra.config import TelegramConfig
from prime_jennie_runtime.jobs.briefing_glue import daily_briefing_report


class _FakeEngine:
    """collect_briefing_data 에만 전달되므로 sentinel 으로 충분."""


@pytest.mark.asyncio
async def test_daily_briefing_report_returns_stats_without_telegram():
    engine = _FakeEngine()
    result = BriefingResult(html="<b>hi</b>", source="fallback", data={})

    with (
        patch(
            "prime_jennie_runtime.briefing.context_builder.collect_briefing_data",
            new=AsyncMock(return_value={"stub": True}),
        ) as m_collect,
        patch(
            "prime_jennie_runtime.jobs.briefing_glue.generate_briefing",
            new=AsyncMock(return_value=result),
        ) as m_generate,
    ):
        async with httpx.AsyncClient() as http:
            out = await daily_briefing_report(
                engine,  # type: ignore[arg-type]
                http,
                telegram_config=None,
            )

    m_collect.assert_awaited_once()
    m_generate.assert_awaited_once()
    assert out == {"source": "fallback", "html_len": 9, "telegram_sent": False}


@pytest.mark.asyncio
async def test_daily_briefing_report_sends_via_telegram_dry_run():
    engine = _FakeEngine()
    result = BriefingResult(html="<b>hi</b>", source="llm", data={})
    tg_cfg = TelegramConfig(bot_token="tok", chat_id="42", dry_run=True)

    with (
        patch(
            "prime_jennie_runtime.briefing.context_builder.collect_briefing_data",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "prime_jennie_runtime.jobs.briefing_glue.generate_briefing",
            new=AsyncMock(return_value=result),
        ),
    ):
        async with httpx.AsyncClient() as http:
            out = await daily_briefing_report(
                engine,  # type: ignore[arg-type]
                http,
                telegram_config=tg_cfg,
            )

    assert out["source"] == "llm"
    assert out["html_len"] == 9
    assert out["telegram_sent"] is True
