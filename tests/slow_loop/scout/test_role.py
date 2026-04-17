"""ScoutRole fast path 구조 테스트."""

from __future__ import annotations

from datetime import date

import pytest
from minyoung_mah import InvocationContext, SubAgentRole

from prime_jennie_runtime.slow_loop.scout.role import ScoutRole
from prime_jennie_runtime.slow_loop.scout.schemas import (
    MacroStateForScout,
    MarketSummary,
    ScoutContext,
    ScoutOutput,
)


def _context() -> ScoutContext:
    return ScoutContext(
        as_of=date(2026, 4, 16),
        universe=["005930", "000660"],
        market_summary=MarketSummary(
            kospi_close=2800.0,
            kospi_change_pct=0.005,
            kosdaq_close=880.0,
            kosdaq_change_pct=0.002,
            up_count=500,
            down_count=300,
        ),
        macro_state=MacroStateForScout(
            gate="open", size_multiplier=0.75, gate_run_id="macro_20260416_0800"
        ),
        news_scores={},
        sector_momentum={"반도체": 0.04},
        strategy_tags_available=["SECTOR_MOMENTUM"],
    )


def test_scout_role_satisfies_subagent_role_protocol():
    """SubAgentRole protocol duck-typing 확인 (tool_allowlist=[], max_iterations=1)."""
    role = ScoutRole()
    assert isinstance(role, SubAgentRole)


def test_scout_role_fast_path_conditions():
    """fast path 필수 조건: tool_allowlist 비어있음 + max_iterations=1 + output_schema 설정."""
    role = ScoutRole()
    assert role.tool_allowlist == []
    assert role.max_iterations == 1
    assert role.output_schema is ScoutOutput
    assert role.model_tier == "strong"


def test_scout_role_system_prompt_contains_allowed_imports():
    role = ScoutRole()
    # 허용 import 리스트가 system prompt에 들어가 있어야
    assert "pandas" in role.system_prompt
    assert "numpy" in role.system_prompt
    assert "sklearn.cluster" in role.system_prompt


def test_scout_role_system_prompt_contains_forbidden_patterns():
    role = ScoutRole()
    assert "eval" in role.system_prompt
    assert "__import__" in role.system_prompt
    assert "미래 데이터" in role.system_prompt


def test_scout_role_build_user_message_requires_context():
    role = ScoutRole()
    inv = InvocationContext(task_summary="scout", user_request="")
    with pytest.raises(ValueError, match="scout_context"):
        role.build_user_message(inv)


def test_scout_role_build_user_message_with_context():
    role = ScoutRole()
    ctx = _context()
    inv = InvocationContext(
        task_summary="scout",
        user_request="",
        metadata={"scout_context": ctx},
    )
    msg = role.build_user_message(inv)
    assert "2026-04-16" in msg
    assert "SECTOR_MOMENTUM" in msg
    assert "반도체" in msg
