"""MacroGateRole fast path 테스트."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from minyoung_mah import InvocationContext, SubAgentRole

from prime_jennie_runtime.slow_loop.macro.feeders.stub import (
    StubKorMacroNewsFeeder,
    StubMarketSnapshotFeeder,
    StubWsjDigestFeeder,
)
from prime_jennie_runtime.slow_loop.macro.role import MacroGateRole
from prime_jennie_runtime.slow_loop.macro.schemas import MacroGateOutput


@pytest.mark.asyncio
async def test_macro_role_satisfies_protocol():
    role = MacroGateRole()
    assert isinstance(role, SubAgentRole)


def test_macro_role_fast_path_conditions():
    role = MacroGateRole()
    assert role.tool_allowlist == []
    assert role.max_iterations == 1
    assert role.output_schema is MacroGateOutput
    assert role.model_tier == "reasoning"


def test_macro_role_system_prompt_has_closed_conditions():
    role = MacroGateRole()
    assert "지정학적 critical" in role.system_prompt
    assert "KRW/USD" in role.system_prompt
    assert "반-어드바이저" not in role.system_prompt  # 설계 메모, 프롬프트에는 없어야
    assert 'gate="open"' in role.system_prompt
    assert "size_multiplier=0.0" in role.system_prompt


def test_macro_role_requires_context():
    role = MacroGateRole()
    inv = InvocationContext(task_summary="macro", user_request="")
    with pytest.raises(ValueError, match="macro_context"):
        role.build_user_message(inv)


@pytest.mark.asyncio
async def test_macro_role_build_user_message():
    from prime_jennie_runtime.slow_loop.macro.context_builder import MacroContextBuilder

    builder = MacroContextBuilder(
        wsj=StubWsjDigestFeeder(),
        market=StubMarketSnapshotFeeder(),
        kor=StubKorMacroNewsFeeder(),
    )
    ctx = await builder.build(datetime(2026, 4, 16, 8, 0, tzinfo=UTC))
    role = MacroGateRole()
    inv = InvocationContext(
        task_summary="macro",
        user_request="",
        metadata={"macro_context": ctx},
    )
    msg = role.build_user_message(inv)
    assert "KOSPI" in msg
    assert "wsj_stub_0000" in msg
    # Trigger 표시 확인
    assert "scheduled_0800" in msg
