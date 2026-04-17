"""generate_briefing — LLM caller 연동 테스트."""

from __future__ import annotations

import pytest

from prime_jennie_runtime.briefing import (
    BriefingResult,
    generate_briefing,
)
from prime_jennie_runtime.briefing.formatters import compute_trade_summary


def _minimal_data() -> dict:
    return {
        "date": "2026-04-17",
        "positions": [],
        "trades": [],
        "trade_summary": compute_trade_summary([]),
        "macro": None,
        "watchlist": [],
        "assets": None,
        "news": [],
    }


@pytest.mark.asyncio
async def test_generate_briefing_uses_fallback_when_no_llm():
    res = await generate_briefing(_minimal_data(), llm_caller=None)
    assert isinstance(res, BriefingResult)
    assert res.source == "fallback"
    assert "<b>[2026-04-17] 일일 브리핑</b>" in res.html


@pytest.mark.asyncio
async def test_generate_briefing_uses_llm_output_when_available():
    captured: dict = {}

    async def fake_llm(system: str, prompt: str) -> str:
        captured["system"] = system
        captured["prompt"] = prompt
        return "<b>[2026-04-17] 제니의 일일 브리핑</b>\n\n시장은 평온했어요."

    res = await generate_briefing(_minimal_data(), llm_caller=fake_llm)
    assert res.source == "llm"
    assert "제니의 일일 브리핑" in res.html
    # system prompt 는 JENNIE_SYSTEM_PROMPT 전체
    assert "Jennie" in captured["system"]
    # user prompt 는 구조화 컨텍스트
    assert "[일일 브리핑 데이터] 2026-04-17" in captured["prompt"]


@pytest.mark.asyncio
async def test_generate_briefing_falls_back_on_llm_exception():
    async def broken_llm(system: str, prompt: str) -> str:
        raise RuntimeError("LLM unavailable")

    res = await generate_briefing(_minimal_data(), llm_caller=broken_llm)
    assert res.source == "fallback"


@pytest.mark.asyncio
async def test_generate_briefing_falls_back_when_llm_returns_none():
    async def null_llm(system: str, prompt: str) -> None:
        return None

    res = await generate_briefing(_minimal_data(), llm_caller=null_llm)
    assert res.source == "fallback"


@pytest.mark.asyncio
async def test_generate_briefing_truncates_to_max_len():
    async def verbose_llm(system: str, prompt: str) -> str:
        return "가" * 5000

    res = await generate_briefing(_minimal_data(), llm_caller=verbose_llm, max_len=3500)
    assert res.source == "llm"
    assert len(res.html) == 3500
