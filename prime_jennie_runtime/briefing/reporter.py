"""일일 브리핑 content generator.

Track B (`daily_briefing_report` job) 가 호출하는 유일한 진입점:
    html = await generate_briefing(data, llm_caller=...)

실제 Telegram 발송은 v3 `telegram_bot` 또는 Track B job 에서 처리 (이 모듈 책임 밖).

포팅 원본: prime-jennie/prime_jennie/services/briefing/reporter.py
(v2 `DailyReporter.create_and_send_report`). v3 에선 LLM 호출을 외부 주입으로 분리.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .formatters import build_llm_context, format_fallback_html
from .prompts import JENNIE_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

LLMCaller = Callable[[str, str], Awaitable[str | None]]
"""async (system_prompt, user_prompt) -> content | None."""


@dataclass(frozen=True)
class BriefingResult:
    html: str
    source: str  # "llm" | "fallback"
    data: dict


async def generate_briefing(
    data: dict,
    *,
    llm_caller: LLMCaller | None = None,
    max_len: int = 3500,
) -> BriefingResult:
    """브리핑 dict → HTML.

    Args:
        data: `collect_briefing_data` 결과 dict.
        llm_caller: async (system, prompt) → content. None 이면 fallback HTML.
        max_len: LLM 응답 최대 길이 (초과 시 잘림).

    Returns:
        BriefingResult(html, source="llm"|"fallback", data)
    """
    context = build_llm_context(data)

    html_text: str | None = None
    if llm_caller is not None:
        try:
            html_text = await llm_caller(JENNIE_SYSTEM_PROMPT, context)
        except Exception:
            logger.exception("LLM briefing generation failed — fallback HTML 사용")
            html_text = None

    if html_text:
        return BriefingResult(html=html_text[:max_len], source="llm", data=data)

    return BriefingResult(html=format_fallback_html(data), source="fallback", data=data)


def build_claude_llm_caller(
    model: Any,
    *,
    max_tokens: int = 4000,
) -> LLMCaller:
    """langchain-anthropic `ChatAnthropic` 인스턴스 → LLMCaller 어댑터.

    slow_loop 이 쓰는 Claude Opus 4.7 모델을 그대로 재사용 가능.
    Track B 는 `_try_build_tiered_router` 로 만든 `reasoning` tier 를 넘겨 쓰면 된다.
    """

    async def _call(system: str, prompt: str) -> str | None:
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
        except ImportError:
            logger.warning("langchain_core 미설치 — briefing LLM 호출 skip")
            return None

        messages = [SystemMessage(content=system), HumanMessage(content=prompt)]
        # langchain ChatAnthropic 은 max_tokens 를 생성자 또는 invoke kwargs 로 받음.
        invoke_fn = getattr(model, "ainvoke", None)
        if invoke_fn is None:
            logger.warning("model.ainvoke 가 없음 — briefing LLM skip")
            return None
        response = await invoke_fn(messages, max_tokens=max_tokens)
        # TODO(llm-stats): briefing 호출도 `prime_jennie_runtime.infra.llm_stats.record_llm_call`
        # 로 service="briefing" 누적. 현재 jobs/app.py 가 llm_caller=None 으로 주입해
        # dead path 라 후행 작업 — llm_caller 주입 활성화 시 response.usage_metadata 로
        # tokens/cost 추출 후 기록.
        content = getattr(response, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [blk.get("text", "") for blk in content if isinstance(blk, dict)]
            return "".join(parts) or None
        return None

    return _call


__all__ = [
    "BriefingResult",
    "LLMCaller",
    "build_claude_llm_caller",
    "generate_briefing",
]
