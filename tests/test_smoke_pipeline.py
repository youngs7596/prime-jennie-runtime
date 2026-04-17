"""Smoke Pipeline 테스트 — minyoung-mah StaticPipeline으로 E2E 검증.

Scout stub → Strategy stub → PositionSheet 발행 → Redis Stream 소비.
FakeChatModel + fakeredis 사용, Docker 불필요.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pytest
from minyoung_mah import (
    InvocationContext,
    NullHITLChannel,
    NullMemoryStore,
    NullObserver,
    Orchestrator,
    PipelineStep,
    RoleRegistry,
    SingleModelRouter,
    StaticPipeline,
    ToolRegistry,
    default_resilience,
)
from pydantic import BaseModel

from prime_jennie_runtime.infra.redis_streams import (
    STREAM_POSITION_SHEETS,
    TypedStreamPublisher,
)
from prime_jennie_runtime.position_sheet.schema import KST, PositionSheet

# =====================================================================
# Scout/Macro Output 스키마
# =====================================================================


class ScoutOutput(BaseModel):
    """Scout 출력 — 스크리닝 코드 + 메타데이터."""

    screening_code: str
    hypothesis: str
    expected_candidates: int
    factor_weights: dict[str, float]
    strategy_tags_used: list[str]


class MacroGateOutput(BaseModel):
    """Macro Gate 출력 — 바이너리 게이트 + 사이즈 배수."""

    gate: str  # "open" | "closed"
    size_multiplier: float
    reasoning: str


# =====================================================================
# Stub Chat Model — 고정 응답
# =====================================================================


class _StructuredHandle:
    def __init__(self, response: BaseModel) -> None:
        self._response = response

    async def ainvoke(self, messages: list[Any]) -> BaseModel:
        return self._response


class StubChatModel:
    """Scout/Macro 고정 응답 모델."""

    def __init__(self) -> None:
        self._responses: dict[type, BaseModel] = {
            ScoutOutput: ScoutOutput(
                screening_code="def screen(md, ctx): return []",
                hypothesis="테스트 가설 — 반도체 모멘텀",
                expected_candidates=5,
                factor_weights={"momentum": 0.4, "value": 0.3, "news": 0.3},
                strategy_tags_used=["SECTOR_MOMENTUM"],
            ),
            MacroGateOutput: MacroGateOutput(
                gate="open",
                size_multiplier=0.75,
                reasoning="시장 안정, VIX 낮음",
            ),
        }

    async def ainvoke(self, messages: list[Any]) -> Any:
        from langchain_core.messages import AIMessage

        return AIMessage(content="stub response")

    def bind_tools(self, tool_defs: list[Any]) -> StubChatModel:
        return self

    def with_structured_output(self, schema: type[BaseModel]) -> _StructuredHandle:
        return _StructuredHandle(self._responses[schema])


# =====================================================================
# Role 정의
# =====================================================================


@dataclass(frozen=True)
class StaticRole:
    name: str
    system_prompt: str
    tool_allowlist: list[str] = field(default_factory=list)
    model_tier: str = "default"
    output_schema: type[BaseModel] | None = None
    max_iterations: int = 1

    def build_user_message(self, invocation: InvocationContext) -> str:
        return invocation.task_summary


# =====================================================================
# 테스트
# =====================================================================


@pytest.mark.asyncio
async def test_smoke_pipeline():
    """Scout → Macro → PositionSheet 생성 E2E."""
    # 1. 역할 등록
    scout_role = StaticRole(
        name="scout",
        system_prompt="Scout stub",
        output_schema=ScoutOutput,
        model_tier="strong",
    )
    macro_role = StaticRole(
        name="macro_gate",
        system_prompt="Macro Gate stub",
        output_schema=MacroGateOutput,
        model_tier="reasoning",
    )

    # 2. 파이프라인 조립
    pipeline = StaticPipeline(
        steps=[
            PipelineStep(
                name="scout",
                role="scout",
                input_mapping=lambda state: InvocationContext(
                    task_summary="스크리닝 코드 생성",
                    user_request="",
                ),
            ),
            PipelineStep(
                name="macro_gate",
                role="macro_gate",
                input_mapping=lambda state: InvocationContext(
                    task_summary="매크로 게이트 판정",
                    user_request="",
                ),
            ),
        ],
    )

    # 3. Orchestrator 실행
    orch = Orchestrator(
        role_registry=RoleRegistry.of(scout_role, macro_role),
        tool_registry=ToolRegistry(),
        model_router=SingleModelRouter(StubChatModel()),
        memory=NullMemoryStore(),
        hitl=NullHITLChannel(),
        observer=NullObserver(),
        resilience=default_resilience(),
    )
    result = await orch.run_pipeline(pipeline, user_request="daily scout run")

    # 4. 결과 검증
    assert result.completed
    scout_out = result.state["scout"].payload_as(ScoutOutput)
    macro_out = result.state["macro_gate"].payload_as(MacroGateOutput)
    assert scout_out is not None
    assert macro_out is not None
    assert macro_out.gate == "open"
    assert macro_out.size_multiplier == 0.75

    # 5. Strategy Engine (결정론) — PositionSheet 생성
    now = datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST)
    sheet = PositionSheet(
        sheet_id="ps_20260416_005930_a3f2",
        generated_at=now,
        valid_until=now + timedelta(hours=6),
        ticker="005930",
        strategy_tag="SECTOR_MOMENTUM",
        size={
            "base_pct": 0.05,
            "macro_multiplier": macro_out.size_multiplier,
            "risk_multiplier": 1.0,
            "final_pct": 0.05 * macro_out.size_multiplier * 1.0,
            "max_notional_krw": 5_000_000,
        },
        entry={
            "trigger": "limit",
            "price": 71200,
            "valid_until": now + timedelta(hours=1),
            "conditions": [],
        },
        exit={
            "rules": [
                {"type": "trailing_tp", "activate_pct": 0.05, "drop_pct": 0.03},
                {"type": "fixed_sl", "pct": 0.05},
                {"type": "time_stop", "mode": "eod"},
            ],
            "priority": "first_match",
        },
        provenance={
            "scout_run_id": "scout_20260416_0900",
            "scout_code_hash": "sha256:stub",
            "scout_hypothesis": scout_out.hypothesis,
            "macro_state_snapshot": {
                "gate": macro_out.gate,
                "size_multiplier": macro_out.size_multiplier,
                "gate_run_id": "macro_20260416_0800",
            },
            "news_score_at_generation": None,
            "strategy_policy_version": "v3.0.1",
            "generated_by": "prime-jennie-runtime@v3.0.1",
        },
    )

    # 6. 시트 검증
    assert sheet.size.final_pct == pytest.approx(0.0375, abs=1e-9)
    assert sheet.provenance.scout_hypothesis == "테스트 가설 — 반도체 모멘텀"

    # 7. JSON 라운드트립
    json_str = sheet.model_dump_json()
    restored = PositionSheet.model_validate_json(json_str)
    assert restored.sheet_id == sheet.sheet_id


@pytest.mark.asyncio
async def test_smoke_redis_publish(fake_redis):
    """PositionSheet → Redis Stream 발행."""
    now = datetime(2026, 4, 16, 9, 15, 0, tzinfo=KST)
    sheet = PositionSheet(
        sheet_id="ps_20260416_005930_b1c2",
        generated_at=now,
        valid_until=now + timedelta(hours=6),
        ticker="005930",
        strategy_tag="SECTOR_MOMENTUM",
        size={
            "base_pct": 0.05,
            "macro_multiplier": 0.75,
            "risk_multiplier": 1.0,
            "final_pct": 0.0375,
            "max_notional_krw": 5_000_000,
        },
        entry={
            "trigger": "limit",
            "price": 71200,
            "valid_until": now + timedelta(hours=1),
        },
        exit={
            "rules": [
                {"type": "fixed_sl", "pct": 0.05},
                {"type": "time_stop", "mode": "eod"},
            ],
        },
        provenance={
            "scout_run_id": "scout_20260416_0900",
            "scout_code_hash": "sha256:test",
            "scout_hypothesis": "테스트",
            "macro_state_snapshot": {
                "gate": "open",
                "size_multiplier": 0.75,
                "gate_run_id": "macro_20260416_0800",
            },
            "strategy_policy_version": "v3.0.1",
            "generated_by": "test",
        },
    )

    publisher = TypedStreamPublisher(fake_redis, STREAM_POSITION_SHEETS, PositionSheet)
    msg_id = await publisher.publish(sheet)
    assert msg_id

    # Redis에서 확인
    length = await fake_redis.xlen(STREAM_POSITION_SHEETS)
    assert length == 1
