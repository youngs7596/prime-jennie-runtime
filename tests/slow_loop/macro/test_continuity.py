"""abrupt_transition 연속성 체크 테스트 (MG07)."""

from __future__ import annotations

from datetime import UTC, datetime

from prime_jennie_runtime.slow_loop.macro.continuity import abrupt_transition
from prime_jennie_runtime.slow_loop.macro.schemas import MacroGateOutput, RecentMacroRun


def _mk_output(size: float, gate: str = "open") -> MacroGateOutput:
    return MacroGateOutput(
        gate=gate,  # type: ignore[arg-type]
        size_multiplier=size,
        reasoning="test",
        top_risks=[],
        confidence="medium",
        news_digest_ref="unavailable",
    )


def _mk_history(size: float) -> RecentMacroRun:
    return RecentMacroRun(
        macro_run_id="macro_20260416_0800",
        generated_at=datetime.now(UTC),
        gate="open",
        size_multiplier=size,
        top_risks_summary="",
    )


def test_mg07_abrupt_1_to_025():
    """MG07: 1.00 → 0.25 (delta 0.75) → 급격 전환."""
    cur = _mk_output(0.25)
    hist = [_mk_history(1.0)]
    assert abrupt_transition(cur, hist) is True


def test_smooth_transition_not_abrupt():
    """0.75 → 0.50 (delta 0.25) → 정상."""
    cur = _mk_output(0.50)
    hist = [_mk_history(0.75)]
    assert abrupt_transition(cur, hist) is False


def test_exactly_half_delta_is_abrupt():
    """delta 0.5 정확히는 급격 (>=)."""
    cur = _mk_output(0.25)
    hist = [_mk_history(0.75)]
    assert abrupt_transition(cur, hist) is True


def test_empty_history_not_abrupt():
    """이력 없으면 항상 False (첫 실행 등)."""
    cur = _mk_output(0.25)
    assert abrupt_transition(cur, []) is False


def test_open_to_closed_equivalent_drop():
    """open 1.00 → closed 0.0 → delta 1.0 → 급격."""
    cur = _mk_output(0.0, gate="closed")
    hist = [_mk_history(1.0)]
    assert abrupt_transition(cur, hist) is True
