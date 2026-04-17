"""LiteLLM 설정 — 4 tier 모델 매핑 + Langfuse callback.

minyoung-mah의 TieredModelRouter와 함께 사용.
LLM-level 트레이싱은 LiteLLM의 callback으로 처리.
"""

from __future__ import annotations

import litellm

from .config import LangfuseConfig, LLMConfig


def configure_litellm(
    llm_config: LLMConfig,
    langfuse_config: LangfuseConfig | None = None,
) -> None:
    """LiteLLM 초기화. Langfuse callback 활성화 (키가 있을 때만)."""
    litellm.drop_params = True

    if langfuse_config and langfuse_config.enabled:
        litellm.success_callback = ["langfuse"]
        litellm.failure_callback = ["langfuse"]


def get_tier_model_map(config: LLMConfig) -> dict[str, str]:
    """tier → LiteLLM 모델 이름 매핑 반환."""
    return {
        "fast": config.fast,
        "default": config.default,
        "strong": config.strong,
        "reasoning": config.reasoning,
    }
