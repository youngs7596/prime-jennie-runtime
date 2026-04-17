"""Macro 판정 연속성 체크.

MACRO_GATE_SPEC §5.3. 한 단계 건너뛴 급격 전환(예: 1.00 → 0.25) 시 로깅.
잘못됐다는 뜻이 아니라 (이란 전쟁 같은 상황은 당연히 급격), 영석 리뷰 시그널.
"""

from __future__ import annotations

from .schemas import MacroGateOutput, RecentMacroRun

ABRUPT_TRANSITION_DELTA = 0.5  # size_multiplier 차이 >= 0.5면 급격 전환


def abrupt_transition(current: MacroGateOutput, history: list[RecentMacroRun]) -> bool:
    """직전 run 대비 size_multiplier가 0.5 이상 변하면 True."""
    if not history:
        return False

    prev = history[0]
    delta = abs(current.size_multiplier - prev.size_multiplier)
    return delta >= ABRUPT_TRANSITION_DELTA
