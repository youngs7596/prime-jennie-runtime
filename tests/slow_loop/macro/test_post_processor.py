"""Macro post_processor 테스트 (MG03, MG04~MG07, MG21 이벤트 발행 포함)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from minyoung_mah import CollectingObserver

from prime_jennie_runtime.slow_loop.macro import post_processor as pp
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


# =====================================================================
# Phase 0 #3: reversal_guard — 같은 KST 거래일 closed → open 역행 차단
# =====================================================================


class _FakeEngine:
    """post_processor 의 reversal_guard 가 engine 을 sentinel 로만 쓰는지 확인용.

    실제 PG 조회는 monkeypatch 로 _fetch_closed_row_today 를 치환해 우회한다.
    """


def _stub_fetch(row: dict | None):
    async def _f(engine, *, current_run_id, as_of):
        return row

    return _f


@pytest.mark.asyncio
async def test_reversal_guard_latches_open_to_closed_when_today_closed_exists(monkeypatch):
    """같은 KST 거래일에 closed row 가 있고 현재 LLM 이 open → closed/0 으로 latch."""
    monkeypatch.delenv("MACRO_REVERSAL_GUARD_DISABLED", raising=False)
    latched_at = datetime(2026, 5, 25, 11, 40, tzinfo=UTC)
    monkeypatch.setattr(
        pp,
        "_fetch_closed_row_today",
        _stub_fetch({"macro_run_id": "mr_20260525_1140_abc", "generated_at": latched_at}),
    )

    obs = CollectingObserver()
    raw = _output(gate="open", size=0.75)
    result = await run_post_processing(
        raw,
        _normal_snapshot(),
        [],
        obs,
        engine=_FakeEngine(),
        macro_run_id="mr_20260525_1342_xyz",
        as_of=datetime(2026, 5, 25, 13, 42, tzinfo=UTC),
    )

    assert result.reversal_latched is True
    assert result.output.gate == "closed"
    assert result.output.size_multiplier == 0.0
    assert result.reversal_meta is not None
    assert result.reversal_meta["original_gate"] == "open"
    assert result.reversal_meta["latched_from_macro_run_id"] == "mr_20260525_1140_abc"
    events = _find_events(obs, "pj.macro.reversal_guard")
    assert events
    assert events[0].metadata["latched_from_macro_run_id"] == "mr_20260525_1140_abc"
    # 자동 closed 이벤트도 같이 발행되어야 한다 (current.gate == closed).
    assert _find_events(obs, "pj.macro.gate_closed")


@pytest.mark.asyncio
async def test_reversal_guard_noop_when_no_closed_today(monkeypatch):
    """같은 거래일에 closed row 가 없으면 latch 미발동, open 유지."""
    monkeypatch.delenv("MACRO_REVERSAL_GUARD_DISABLED", raising=False)
    monkeypatch.setattr(pp, "_fetch_closed_row_today", _stub_fetch(None))

    obs = CollectingObserver()
    raw = _output(gate="open", size=0.75)
    result = await run_post_processing(
        raw,
        _normal_snapshot(),
        [],
        obs,
        engine=_FakeEngine(),
        macro_run_id="mr_20260525_1142_a",
        as_of=datetime(2026, 5, 25, 11, 42, tzinfo=UTC),
    )

    assert result.reversal_latched is False
    assert result.reversal_meta is None
    assert result.output.gate == "open"
    assert _find_events(obs, "pj.macro.reversal_guard") == []


@pytest.mark.asyncio
async def test_reversal_guard_disabled_env_bypass(monkeypatch):
    """MACRO_REVERSAL_GUARD_DISABLED=1 이면 closed row 가 있어도 latch 미발동."""
    monkeypatch.setenv("MACRO_REVERSAL_GUARD_DISABLED", "1")
    fetch_called = {"count": 0}

    async def _f(engine, *, current_run_id, as_of):
        fetch_called["count"] += 1
        return {"macro_run_id": "should_not_be_used", "generated_at": datetime.now(UTC)}

    monkeypatch.setattr(pp, "_fetch_closed_row_today", _f)

    obs = CollectingObserver()
    raw = _output(gate="open", size=0.75)
    result = await run_post_processing(
        raw,
        _normal_snapshot(),
        [],
        obs,
        engine=_FakeEngine(),
        macro_run_id="mr_test",
        as_of=datetime(2026, 5, 25, 13, 42, tzinfo=UTC),
    )

    assert result.reversal_latched is False
    assert result.output.gate == "open"
    # bypass env 면 DB 조회 자체를 안 한다 (early return).
    assert fetch_called["count"] == 0
    assert _find_events(obs, "pj.macro.reversal_guard") == []


@pytest.mark.asyncio
async def test_reversal_guard_skip_when_engine_missing(monkeypatch):
    """engine 미주입 (smoke/unit) 이면 latch skip — fetch 호출 자체 안 일어남."""
    monkeypatch.delenv("MACRO_REVERSAL_GUARD_DISABLED", raising=False)
    fetch_called = {"count": 0}

    async def _f(engine, *, current_run_id, as_of):  # pragma: no cover - 이 경로는 호출되면 안 됨
        fetch_called["count"] += 1
        return None

    monkeypatch.setattr(pp, "_fetch_closed_row_today", _f)

    obs = CollectingObserver()
    raw = _output(gate="open", size=0.75)
    result = await run_post_processing(raw, _normal_snapshot(), [], obs)

    assert result.reversal_latched is False
    assert fetch_called["count"] == 0


@pytest.mark.asyncio
async def test_reversal_guard_skip_when_current_already_closed(monkeypatch):
    """현재 LLM 이 이미 closed 면 latch 의미 없으므로 fetch 안 함."""
    monkeypatch.delenv("MACRO_REVERSAL_GUARD_DISABLED", raising=False)
    fetch_called = {"count": 0}

    async def _f(engine, *, current_run_id, as_of):  # pragma: no cover
        fetch_called["count"] += 1
        return None

    monkeypatch.setattr(pp, "_fetch_closed_row_today", _f)

    obs = CollectingObserver()
    raw = _output(gate="closed", size=0.0)
    result = await run_post_processing(
        raw,
        _normal_snapshot(),
        [],
        obs,
        engine=_FakeEngine(),
        macro_run_id="mr_test",
        as_of=datetime(2026, 5, 25, 13, 42, tzinfo=UTC),
    )

    assert result.reversal_latched is False
    assert result.output.gate == "closed"
    assert fetch_called["count"] == 0


@pytest.mark.asyncio
async def test_reversal_guard_after_auto_override_no_double_latch(monkeypatch):
    """auto_override 가 먼저 closed 로 바꿔두면 latch 는 다시 안 박힌다."""
    monkeypatch.delenv("MACRO_REVERSAL_GUARD_DISABLED", raising=False)
    monkeypatch.setattr(
        pp,
        "_fetch_closed_row_today",
        _stub_fetch({"macro_run_id": "mr_prev_closed", "generated_at": datetime.now(UTC)}),
    )

    snap = _normal_snapshot().model_copy(update={"usd_krw_change_pct": 0.035})
    assert "fx_shock" in check_closed_conditions(snap)

    obs = CollectingObserver()
    raw = _output(gate="open", size=0.75)
    result = await run_post_processing(
        raw,
        snap,
        [],
        obs,
        engine=_FakeEngine(),
        macro_run_id="mr_test",
        as_of=datetime(2026, 5, 25, 13, 42, tzinfo=UTC),
    )

    assert result.auto_override_applied is True
    assert result.reversal_latched is False  # auto_override 가 이미 처리
    assert result.output.gate == "closed"
