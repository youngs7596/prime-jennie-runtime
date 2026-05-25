"""LLM provider 가격표 + shadow/actual 비용 환산.

actual cost — 각 서비스가 record_llm_call 시 넘긴 cost_usd 의 합산. vLLM
(news_analysis) 은 self-hosted GPU 라 호출당 cost 가 0 으로 기록되므로, 별도
amortized 일일 비용을 vllm_amortized_daily_usd 필드로 분리해 응답에 노출한다.
프론트가 UI 에서 명시적으로 더한다.

shadow cost — 모든 호출을 DeepSeek-chat 으로 라우팅했다고 가정했을 때의 환산
비용 (1M token 당 가격 × token 수). 현재 라우팅 (Macro/Briefing = DeepSeek,
News = vLLM) 의 비용 절감 효과를 운영자가 한눈에 볼 수 있게 한다.

향후 config/llm_pricing.yaml hot-reload 로 갈 여지가 있지만, 가격 변경 빈도가
낮아 현재는 Python module 로만 둔다.
"""

from __future__ import annotations

PRICING: dict[str, dict[str, dict[str, float]]] = {
    "deepseek": {
        "chat": {"in_per_million_usd": 0.27, "out_per_million_usd": 1.10},
    },
    "openai": {
        "gpt-4o-mini": {"in_per_million_usd": 0.15, "out_per_million_usd": 0.60},
    },
    "anthropic": {
        # claude-opus-4-7 1M token 가격 (참고용, 현재 라우팅에서는 macro_shadow 만).
        "claude-opus-4-7": {"in_per_million_usd": 15.0, "out_per_million_usd": 75.0},
    },
}

# vLLM (RTX 3090 24/7) 일일 amortized 비용 추정.
# - 전력: 220W × 24h × $0.15/kWh ≈ $0.79
# - 감가: $1500 / (5 년 × 365 일) ≈ $0.82
# 합 ≈ $1.6. 운영 마진 포함 보수적으로 $2 로 박는다.
VLLM_AMORTIZED_DAILY_USD: float = 2.0

# shadow 환산 default — Macro / News / Briefing 등 어떤 호출을 DeepSeek 로
# 옮겼을 때를 가정. 비교 기준이 단일이어야 운영자가 해석 가능.
SHADOW_PROVIDER: str = "deepseek"
SHADOW_MODEL: str = "chat"


def calc_shadow_cost(
    tok_in: int,
    tok_out: int,
    provider: str = SHADOW_PROVIDER,
    model: str = SHADOW_MODEL,
) -> float:
    """tok_in / tok_out 을 가격표로 환산해 USD 비용 반환. 모델 누락 시 0."""
    price = PRICING.get(provider, {}).get(model)
    if price is None:
        return 0.0
    return (
        tok_in * price["in_per_million_usd"] + tok_out * price["out_per_million_usd"]
    ) / 1_000_000
