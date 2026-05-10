"""Macro post_processor 테스트 (MG03, MG04~MG07, MG21 이벤트 발행 포함)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from minyoung_mah import CollectingObserver

from prime_jennie_runtime.slow_loop.macro.closed_conditions import check_closed_conditions
from prime_jennie_runtime.slow_loop.macro.post_processor import run_post_processing
from prime_jennie_runtime.slow_loop.macro.schemas import (
    IndexPoint,
    MacroGateOutput,
    MarketSnapshot,
    RecentMacroRun,
    SectorDrop,
)


def _normal_snapshot() -> MarketSnapshot:
    ip = IndexPoint(close=2800.0, change_pct=0.005)
    return MarketSnapshot(
        as_of=datetime.now(UTC),
        kospi=ip,
        kosdaq=ip,
        sp500=ip,
        nasdaq=ip,
        nikkei=ip,
        hsi=ip,
        usd_krw=1350.0,
        usd_krw_change_pct=0.002,
        usd_jpy=150.0,
        crude_oil=80.0,
        crude_oil_change_pct=0.0,
        gold=2300.0,
        gold_change_pct=0.0,
        vix=18.0,
        kospi_20d_vol=0.15,
        kospi_60d_vol=0.14,
        major_sector_drops=[],
    )


def _output(gate: str = "open", size: float = 0.75) -> MacroGateOutput:
    return MacroGateOutput(
        gate=gate,  # type: ignore[arg-type]
        size_multiplier=size,
        reasoning="test reasoning",
        top_risks=[],
        confidence="medium",
        news_digest_ref="wsj_test",
    )


def _find_events(obs: CollectingObserver, name: str) -> list:
    return [e for e in obs.events if e.name == name]


# =====================================================================
# 기본: 정상 → discretize만 적용
# =====================================================================


@pytest.mark.asyncio
async def test_normal_open_discretized():
    obs = CollectingObserver()
    raw = _output(gate="open", size=0.73)  # MG04: 0.73 → 0.75
    result = await run_post_processing(raw, _normal_snapshot(), [], obs)
    assert result.output.gate == "open"
    assert result.output.size_multiplier == 0.75
    assert not result.auto_override_applied
    assert not result.inconsistent_open_zero
    # 정상 open이면 gate_closed 이벤트 없음
    assert _find_events(obs, "pj.macro.gate_closed") == []


@pytest.mark.asyncio
async def test_normal_closed_emits_gate_closed():
    obs = CollectingObserver()
    raw = _output(gate="closed", size=0.3)  # MG06: closed면 0.0 강제
    result = await run_post_processing(raw, _normal_snapshot(), [], obs)
    assert result.output.gate == "closed"
    assert result.output.size_multiplier == 0.0
    assert _find_events(obs, "pj.macro.gate_closed")


# =====================================================================
# MG03: LLM이 closed 조건 놓침 → auto_override
# =====================================================================


@pytest.mark.asyncio
async def test_mg03_auto_override_on_fx_shock():
    """LLM이 open 출력했지만 fx_shock 트리거 → 강제 closed."""
    obs = CollectingObserver()
    snap = _normal_snapshot()
    snap = snap.model_copy(update={"usd_krw_change_pct": 0.035})
    # 트리거 확인
    assert "fx_shock" in check_closed_conditions(snap)

    raw = _output(gate="open", size=0.75)
    result = await run_post_processing(raw, snap, [], obs)
    assert result.auto_override_applied is True
    assert result.output.gate == "closed"
    assert result.output.size_multiplier == 0.0
    # 이벤트 순서 확인
    assert _find_events(obs, "pj.macro.auto_override")
    assert _find_events(obs, "pj.macro.gate_closed")
    override_event = _find_events(obs, "pj.macro.auto_override")[0]
    assert "fx_shock" in override_event.metadata["triggers"]


@pytest.mark.asyncio
async def test_no_override_when_llm_already_closed():
    """LLM이 이미 closed면 override 불필요."""
    obs = CollectingObserver()
    snap = _normal_snapshot().model_copy(update={"usd_krw_change_pct": 0.035})
    raw = _output(gate="closed", size=0.0)
    result = await run_post_processing(raw, snap, [], obs)
    assert result.auto_override_applied is False
    assert _find_events(obs, "pj.macro.auto_override") == []


# =====================================================================
# MG21: open + 0.0 모순 → 0.25 + inconsistent_open_zero 이벤트
# =====================================================================


@pytest.mark.asyncio
async def test_mg21_inconsistent_open_zero():
    obs = CollectingObserver()
    raw = _output(gate="open", size=0.0)
    result = await run_post_processing(raw, _normal_snapshot(), [], obs)
    assert result.inconsistent_open_zero is True
    assert result.output.size_multiplier == 0.75
    events = _find_events(obs, "pj.macro.inconsistent_open_zero")
    assert events
    assert events[0].metadata["forced_to"] == 0.75


# =====================================================================
# MG07: abrupt transition 이벤트
# =====================================================================


@pytest.mark.asyncio
async def test_mg07_abrupt_transition_event():
    """전일 open 1.0 → 오늘 closed 0.0 같은 급격 전환은 abrupt 이벤트."""
    obs = CollectingObserver()
    raw = _output(gate="closed", size=0.0)  # ← 전일 1.00 → closed 로 급락
    history = [
        RecentMacroRun(
            macro_run_id="macro_prev",
            generated_at=datetime.now(UTC),
            gate="open",
            size_multiplier=1.0,
        )
    ]
    result = await run_post_processing(raw, _normal_snapshot(), history, obs)
    assert result.abrupt is True
    events = _find_events(obs, "pj.macro.abrupt_transition")
    assert events
    assert events[0].metadata["prev_size"] == 1.0
    assert events[0].metadata["new_size"] == 0.0


@pytest.mark.asyncio
async def test_smooth_no_abrupt_event():
    obs = CollectingObserver()
    raw = _output(gate="open", size=0.75)
    history = [
        RecentMacroRun(
            macro_run_id="m",
            generated_at=datetime.now(UTC),
            gate="open",
            size_multiplier=1.0,
        )
    ]
    result = await run_post_processing(raw, _normal_snapshot(), history, obs)
    assert result.abrupt is False
    assert _find_events(obs, "pj.macro.abrupt_transition") == []


# =====================================================================
# bypass: MACRO_AUTO_OVERRIDE_DISABLED=1 → triggers 무시, 원본 gate 유지
# =====================================================================


@pytest.mark.asyncio
async def test_auto_override_bypass_env_keeps_open(monkeypatch):
    monkeypatch.setenv("MACRO_AUTO_OVERRIDE_DISABLED", "1")
    obs = CollectingObserver()
    snap = _normal_snapshot().model_copy(update={"usd_krw_change_pct": 0.035})
    assert "fx_shock" in check_closed_conditions(snap)

    raw = _output(gate="open", size=0.75)
    result = await run_post_processing(raw, snap, [], obs)

    assert result.auto_override_applied is False
    assert result.output.gate == "open"
    assert result.output.size_multiplier == 0.75
    assert result.closed_triggers == ()
    assert _find_events(obs, "pj.macro.auto_override") == []
    assert _find_events(obs, "pj.macro.gate_closed") == []
    bypassed = _find_events(obs, "pj.macro.auto_override_bypassed")
    assert bypassed
    assert "fx_shock" in bypassed[0].metadata["triggers"]


# =====================================================================
# 결합: sector_contagion + auto_override
# =====================================================================


@pytest.mark.asyncio
async def test_sector_contagion_triggers_override():
    obs = CollectingObserver()
    snap = _normal_snapshot().model_copy(
        update={
            "major_sector_drops": [
                SectorDrop(sector="반도체", change_pct=-0.06),
                SectorDrop(sector="2차전지", change_pct=-0.07),
                SectorDrop(sector="바이오", change_pct=-0.08),
            ]
        }
    )
    raw = _output(gate="open", size=1.0)
    result = await run_post_processing(raw, snap, [], obs)
    assert result.auto_override_applied is True
    assert "sector_contagion" in result.closed_triggers
