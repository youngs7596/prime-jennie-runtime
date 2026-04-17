"""Macro Gate 후처리 — LLM raw 출력 → 최종 MacroGateOutput.

MACRO_GATE_SPEC §5. 순서:
1. check_closed_conditions(snap) — 결정론 closed 조건 재검증
2. LLM이 open인데 트리거 있으면 → closed 강제 (auto_override)
3. discretize(size_multiplier, gate) — 이산화. open+0.0 모순은 0.25로
4. abrupt_transition 체크 (경고 이벤트)
5. pj.macro.gate_closed / pj.macro.auto_override / pj.macro.inconsistent_open_zero / pj.macro.abrupt_transition 이벤트 발행

반-어드바이저 원칙: reasoning/top_risks/confidence는 로깅 전용, 실행 참조 금지.
이 함수는 실행 경로용 gate, size_multiplier만 건드린다.
"""

from __future__ import annotations

from dataclasses import dataclass

from minyoung_mah import Observer

from prime_jennie_runtime.infra.observer_impl import pj_event

from .closed_conditions import check_closed_conditions
from .continuity import abrupt_transition
from .discretize import discretize_sync
from .schemas import MacroGateOutput, MarketSnapshot, RecentMacroRun


@dataclass(frozen=True)
class MacroPostResult:
    """후처리 산출. auto_override_applied는 macro_runs 테이블 컬럼."""

    output: MacroGateOutput
    auto_override_applied: bool = False
    inconsistent_open_zero: bool = False
    abrupt: bool = False
    closed_triggers: tuple[str, ...] = ()


async def run_post_processing(
    raw: MacroGateOutput,
    snapshot: MarketSnapshot,
    history: list[RecentMacroRun],
    observer: Observer,
) -> MacroPostResult:
    """LLM 원본을 받아 auto_override + discretize + 이벤트 발행까지 수행."""
    triggers = tuple(check_closed_conditions(snapshot))
    auto_override_applied = False

    current = raw

    # 1. auto-override
    if triggers and current.gate == "open":
        current = current.model_copy(
            update={
                "gate": "closed",
                "size_multiplier": 0.0,
                "reasoning": (
                    f"[AUTO-OVERRIDE] 자동 closed 조건 충족: {list(triggers)}. "
                    f"원본 LLM 판단: {current.reasoning}"
                ),
            }
        )
        auto_override_applied = True
        await observer.emit(
            pj_event(
                "pj.macro.auto_override",
                role="macro_gate",
                ok=True,
                triggers=list(triggers),
            )
        )

    # 2. size 이산화 (open+0.0 모순 플래그 수집)
    inconsistent_flag = {"fired": False}

    def _hook() -> None:
        inconsistent_flag["fired"] = True

    new_size = discretize_sync(
        current.size_multiplier,
        current.gate,  # type: ignore[arg-type]
        inconsistent_hook=_hook,
    )
    current = current.model_copy(update={"size_multiplier": new_size})

    if inconsistent_flag["fired"]:
        await observer.emit(
            pj_event(
                "pj.macro.inconsistent_open_zero",
                role="macro_gate",
                ok=False,
                original=raw.size_multiplier,
                forced_to=0.25,
            )
        )

    # 3. gate_closed 이벤트 (closed인 모든 경우)
    if current.gate == "closed":
        await observer.emit(
            pj_event(
                "pj.macro.gate_closed",
                role="macro_gate",
                ok=True,
                triggers=list(triggers),
                auto_override_applied=auto_override_applied,
            )
        )

    # 4. abrupt transition
    abrupt = abrupt_transition(current, history)
    if abrupt:
        prev_size = history[0].size_multiplier if history else None
        await observer.emit(
            pj_event(
                "pj.macro.abrupt_transition",
                role="macro_gate",
                ok=True,
                prev_size=prev_size,
                new_size=current.size_multiplier,
            )
        )

    return MacroPostResult(
        output=current,
        auto_override_applied=auto_override_applied,
        inconsistent_open_zero=inconsistent_flag["fired"],
        abrupt=abrupt,
        closed_triggers=triggers,
    )
