"""Macro Gate 후처리 — LLM raw 출력 → 최종 MacroGateOutput.

MACRO_GATE_SPEC §5. 순서:
1. check_closed_conditions(snap) — 결정론 closed 조건 재검증
2. LLM이 open인데 트리거 있으면 → closed 강제 (auto_override)
3. 같은 KST 거래일에 이미 closed row 가 있으면 현재 open 도 closed 로 latch (reversal_guard)
4. discretize(size_multiplier, gate) — 이산화. open 은 2단계 (0.75 / 1.0). open+0.0 모순은 0.75 로
5. abrupt_transition 체크 (경고 이벤트)
6. pj.macro.gate_closed / pj.macro.auto_override / pj.macro.reversal_guard
   / pj.macro.inconsistent_open_zero / pj.macro.abrupt_transition 이벤트 발행

반-어드바이저 원칙: reasoning/top_risks/confidence는 로깅 전용, 실행 참조 금지.
이 함수는 실행 경로용 gate, size_multiplier만 건드린다.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from minyoung_mah import Observer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from prime_jennie_runtime.infra.observer_impl import pj_event

from .closed_conditions import check_closed_conditions
from .continuity import abrupt_transition
from .discretize import OPEN_FLOOR, discretize_sync
from .schemas import MacroGateOutput, MarketSnapshot, RecentMacroRun

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MacroPostResult:
    """후처리 산출. auto_override_applied는 macro_runs 테이블 컬럼."""

    output: MacroGateOutput
    auto_override_applied: bool = False
    inconsistent_open_zero: bool = False
    abrupt: bool = False
    closed_triggers: tuple[str, ...] = ()
    reversal_latched: bool = False
    # latch 메타 — persistence 가 macro_runs.metadata_json 에 병합 저장.
    # latch 발동 시 키: reversal_guard / original_gate / latched_from_macro_run_id /
    #   latched_from_generated_at
    # paper 완화 시 키 (P2.7): paper_relaxed / would_have_been_gate / original_gate /
    #   latched_from_macro_run_id / latched_from_generated_at
    reversal_meta: dict[str, Any] | None = None


async def _fetch_closed_row_today(
    engine: AsyncEngine,
    *,
    current_run_id: str,
    as_of: datetime,
) -> dict[str, Any] | None:
    """같은 KST 거래일(09:00~다음날 같은 시각 아님, 달력일 기준) 에 gate=closed 인
    가장 최근 macro_runs row 1건을 조회한다.

    포함: auto_override 로 closed 된 row + LLM 자체 closed row 둘 다.
    제외: 현재 run_id (아직 commit 전이라 보통 없지만 안전을 위해).

    실패 시 None — fail-open. 호출자가 latch 미발동.
    """
    sql = text(
        """
        SELECT macro_run_id, generated_at
        FROM macro_runs
        WHERE gate = 'closed'
          AND macro_run_id <> :current_id
          AND (generated_at AT TIME ZONE 'Asia/Seoul')::date
              = (:as_of AT TIME ZONE 'Asia/Seoul')::date
        ORDER BY generated_at DESC
        LIMIT 1
        """
    )
    try:
        async with engine.begin() as conn:
            res = await conn.execute(sql, {"current_id": current_run_id, "as_of": as_of})
            row = res.mappings().first()
    except Exception:
        logger.exception("reversal latch query failed: current_id=%s", current_run_id)
        return None
    return dict(row) if row is not None else None


async def run_post_processing(
    raw: MacroGateOutput,
    snapshot: MarketSnapshot,
    history: list[RecentMacroRun],
    observer: Observer,
    *,
    engine: AsyncEngine | None = None,
    macro_run_id: str | None = None,
    as_of: datetime | None = None,
    paper_mode: bool = False,
) -> MacroPostResult:
    """LLM 원본을 받아 auto_override + reversal_guard + discretize + 이벤트 발행까지 수행.

    engine/macro_run_id/as_of 셋 다 있어야 reversal_guard 가 동작한다 (DB 조회 필요).
    smoke/unit test 등에서 일부가 None 이면 latch skip — auto_override 와 discretize 는 정상.

    paper_mode (P2.7, 2026-06-03): 매매가 없는 paper 모드 (control STOP) 에서는
    reversal latch 로 게이트를 닫지 않는다 — latch 가 시트 발행을 막으면 alpha 측정
    데이터가 줄기 때문. 대신 "실거래였으면 closed 였음" 을 메타로 남겨 latch 가 alpha
    에 주는 영향을 나중에 측정한다. auto_override (결정론 closed 조건) 는 paper 모드
    에서도 그대로 동작한다 — 정당한 국면 신호는 유지.
    """
    triggers = tuple(check_closed_conditions(snapshot))
    auto_override_applied = False

    current = raw

    if triggers and os.environ.get("MACRO_AUTO_OVERRIDE_DISABLED") == "1":
        await observer.emit(
            pj_event(
                "pj.macro.auto_override_bypassed",
                role="macro_gate",
                ok=True,
                triggers=list(triggers),
            )
        )
        triggers = ()

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

    # 1.5 reversal_guard — 같은 KST 거래일에 이미 closed row 가 있다면 open 으로 되돌리지 않는다.
    # auto_override 가 이미 closed 로 바꿔놓은 경우엔 자연스럽게 통과 (current.gate == "closed").
    reversal_latched = False
    reversal_meta: dict[str, Any] | None = None
    if (
        current.gate == "open"
        and os.environ.get("MACRO_REVERSAL_GUARD_DISABLED") != "1"
        and engine is not None
        and macro_run_id is not None
        and as_of is not None
    ):
        row = await _fetch_closed_row_today(engine, current_run_id=macro_run_id, as_of=as_of)
        if row is not None:
            latched_id = row["macro_run_id"]
            latched_at = row["generated_at"]
            original_gate = current.gate
            latched_at_str = (
                latched_at.isoformat() if isinstance(latched_at, datetime) else str(latched_at)
            )
            if paper_mode:
                # P2.7 paper 완화 — 게이트는 open 유지 (시트 발행 허용), 메타만 기록.
                # reversal_latched 는 False 로 둔다 (실제로 latch 가 게이트를 바꾸지 않았으므로).
                reversal_meta = {
                    "paper_relaxed": True,
                    "would_have_been_gate": "closed",
                    "original_gate": original_gate,
                    "latched_from_macro_run_id": latched_id,
                    "latched_from_generated_at": latched_at_str,
                }
                await observer.emit(
                    pj_event(
                        "pj.macro.reversal_guard_paper_relaxed",
                        role="macro_gate",
                        ok=True,
                        original_gate=original_gate,
                        latched_from_macro_run_id=latched_id,
                    )
                )
            else:
                current = current.model_copy(
                    update={
                        "gate": "closed",
                        "size_multiplier": 0.0,
                        "reasoning": (
                            f"[REVERSAL-GUARD] 같은 KST 거래일 closed latch "
                            f"(from {latched_id}). 원본 LLM 판단: {current.reasoning}"
                        ),
                    }
                )
                reversal_latched = True
                reversal_meta = {
                    "reversal_guard": True,
                    "original_gate": original_gate,
                    "latched_from_macro_run_id": latched_id,
                    "latched_from_generated_at": latched_at_str,
                }
                await observer.emit(
                    pj_event(
                        "pj.macro.reversal_guard",
                        role="macro_gate",
                        ok=True,
                        original_gate=original_gate,
                        latched_from_macro_run_id=latched_id,
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
                forced_to=OPEN_FLOOR,
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
        reversal_latched=reversal_latched,
        reversal_meta=reversal_meta,
    )
