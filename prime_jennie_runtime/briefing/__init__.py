"""Prime Jennie v3 일일 브리핑 (content generator).

Track B jobs 가 `generate_briefing(data, llm_caller=...)` 로 호출한다. 실제 Telegram
발송은 `telegram_bot` 에서 처리.
"""

from .context_builder import collect_briefing_data
from .formatters import (
    build_llm_context,
    compute_trade_summary,
    format_fallback_html,
)
from .prompts import JENNIE_SYSTEM_PROMPT
from .reporter import (
    BriefingResult,
    LLMCaller,
    build_chat_llm_caller,
    generate_briefing,
)

__all__ = [
    "BriefingResult",
    "JENNIE_SYSTEM_PROMPT",
    "LLMCaller",
    "build_chat_llm_caller",
    "build_llm_context",
    "collect_briefing_data",
    "compute_trade_summary",
    "format_fallback_html",
    "generate_briefing",
]
