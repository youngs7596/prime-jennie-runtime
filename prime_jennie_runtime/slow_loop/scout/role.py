"""ScoutRole — minyoung-mah SubAgentRole duck-type.

SCOUT_CODE_GENERATION §1.4 tier: STRONG. structured fast path:
  - output_schema=ScoutOutput
  - tool_allowlist=[]  (fast path 필수 조건)
  - max_iterations=1   (fast path 필수 조건)

Orchestrator가 tool_allowlist가 비어있지 않거나 max_iterations>1이면 tool-calling
루프로 빠진다. 두 조건 모두 지켜야 structured output 단일 호출.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from minyoung_mah import InvocationContext
from pydantic import BaseModel

from .prompts import SCOUT_SYSTEM_PROMPT, build_user_prompt
from .schemas import ScoutContext, ScoutOutput


@dataclass(frozen=True)
class ScoutRole:
    """Scout SubAgentRole."""

    name: str = "scout"
    system_prompt: str = SCOUT_SYSTEM_PROMPT
    tool_allowlist: list[str] = field(default_factory=list)
    model_tier: str = "strong"
    output_schema: type[BaseModel] | None = ScoutOutput
    max_iterations: int = 1

    def build_user_message(self, invocation: InvocationContext) -> str:
        """InvocationContext.metadata["scout_context"]에 ScoutContext를 담아야 한다."""
        ctx = invocation.metadata.get("scout_context")
        if not isinstance(ctx, ScoutContext):
            raise ValueError(
                "ScoutRole requires invocation.metadata['scout_context'] of type ScoutContext"
            )
        return build_user_prompt(ctx)
