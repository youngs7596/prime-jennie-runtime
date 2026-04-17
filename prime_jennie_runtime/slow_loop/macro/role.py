"""MacroGateRole — minyoung-mah SubAgentRole duck-type.

MACRO_GATE_SPEC §1.4 tier: REASONING. Scout와 동일한 fast path 패턴:
  - output_schema=MacroGateOutput
  - tool_allowlist=[]
  - max_iterations=1
"""

from __future__ import annotations

from dataclasses import dataclass, field

from minyoung_mah import InvocationContext
from pydantic import BaseModel

from .prompts import MACRO_SYSTEM_PROMPT, build_user_prompt
from .schemas import MacroContext, MacroGateOutput


@dataclass(frozen=True)
class MacroGateRole:
    """Macro Gate SubAgentRole."""

    name: str = "macro_gate"
    system_prompt: str = MACRO_SYSTEM_PROMPT
    tool_allowlist: list[str] = field(default_factory=list)
    model_tier: str = "reasoning"
    output_schema: type[BaseModel] | None = MacroGateOutput
    max_iterations: int = 1

    def build_user_message(self, invocation: InvocationContext) -> str:
        ctx = invocation.metadata.get("macro_context")
        if not isinstance(ctx, MacroContext):
            raise ValueError(
                "MacroGateRole requires invocation.metadata['macro_context'] of type MacroContext"
            )
        return build_user_prompt(ctx)
