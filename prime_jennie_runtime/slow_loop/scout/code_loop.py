"""Scout 코드 생성·검증 닫힌 루프 (2026-04-25 신설).

기존 흐름은 "Scout LLM 1회 호출 → ScoutOutput → screening 1회 실행 → 실패해도 빈
candidates 로 조용히 종료" 였다. LLM 이 numpy import 를 빼먹거나 factors 에
잘못된 타입을 넣는 등의 일관된 실수를 잡지 못해 시트 0 발행이 자주 발생.

새 흐름: **샌드박스 실행을 검증으로 활용하는 닫힌 루프**.

1. Scout LLM 호출 → 코드 생성
2. ``ScreeningToolAdapter.run()`` 으로 격리 실행 → ``ScreeningResult`` 받음
3. ``ok=True`` 이고 candidates 가 있으면 종료
4. 실패면 (sandbox 실행 에러 또는 빈 candidates) 시도 정보를 누적하고 재호출
   ``ScoutContext.previous_attempts`` 에 직전 시도들의 (error, details, code 발췌)
   를 실어서 LLM 이 같은 실수 반복하지 않도록 user prompt 에 직접 노출
5. 최대 ``max_attempts`` 회 (default 3)
6. 같은 ``code_hash`` 가 두 번째로 생성되면 즉시 break (LLM 이 피드백을 무시하고
   같은 코드를 다시 만든 경우 — minyoung-mah ProgressGuard 사상)
7. mah ``Observer`` 에 매 시도마다 ``pj.scout.code_attempt`` 이벤트 emit, 최종
   실패 시 ``pj.scout.escalate`` 까지

DB 는 건드리지 않음. caller (pipeline.py) 가 결과를 받아 persist/publish 한다.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from minyoung_mah import Observer, Orchestrator

from prime_jennie_runtime.infra.observer_impl import pj_event
from prime_jennie_runtime.screening_executor.adapter import ScreeningToolAdapter
from prime_jennie_runtime.screening_executor.schemas import ScreeningResult

from .pipeline_helper import run_scout_once
from .schemas import CodeAttemptHint, ScoutContext, ScoutOutput

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 3
CODE_EXCERPT_LINES = 30


@dataclass(frozen=True)
class CodeAttempt:
    """검증 루프 한 회차 결과."""

    attempt_no: int
    scout_out: ScoutOutput
    scout_step_result: Any  # PipelineStepResult — usage/cost metadata 추출용
    code_hash: str
    result: ScreeningResult


@dataclass(frozen=True)
class ValidatedScoutResult:
    """``generate_validated_code`` 의 최종 산출."""

    scout_out: ScoutOutput  # 마지막 시도의 ScoutOutput (성공 또는 마지막 실패)
    scout_step_result: Any  # 마지막 시도의 step result (None 가능 — output_missing)
    result: ScreeningResult  # 마지막 시도의 ScreeningResult
    attempts: list[CodeAttempt]  # 모든 시도 기록 (1+ 개)
    accepted: bool  # ok=True AND candidates 비어있지 않음
    early_break_reason: str | None = None  # "code_repeated" 등 조기 종료 사유


def _excerpt(code: str, max_lines: int = CODE_EXCERPT_LINES) -> str:
    lines = code.splitlines()
    if len(lines) <= max_lines:
        return code
    return "\n".join(lines[:max_lines]) + f"\n# ... ({len(lines) - max_lines}줄 생략)"


def _hint_from_attempt(a: CodeAttempt) -> CodeAttemptHint:
    error = a.result.error or ("empty_candidates" if not a.result.candidates else "ok_no_data")
    details_raw = a.result.details
    if isinstance(details_raw, list):
        details = "\n".join(str(x) for x in details_raw)
    elif details_raw is None:
        details = "(no details)"
    else:
        details = str(details_raw)
    return CodeAttemptHint(
        attempt_no=a.attempt_no,
        error=error,
        details=details,
        code_excerpt=_excerpt(a.scout_out.screening_code),
    )


async def generate_validated_code(
    *,
    orch: Orchestrator,
    scout_ctx: ScoutContext,
    screening: ScreeningToolAdapter,
    screening_context: dict,
    observer: Observer,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> ValidatedScoutResult:
    """Scout LLM ↔ sandbox 닫힌 루프.

    매 시도마다 ``ScoutContext.previous_attempts`` 에 누적 hint 를 실어 LLM 호출.
    ``screening.run()`` 으로 sandbox 실행 후 결과 평가:

    - ``ok=True`` AND ``candidates`` 비어있지 않음 → 성공, 즉시 반환
    - 그 외 (실행 에러 또는 빈 candidates) → 다음 attempt 로
    - 같은 ``code_hash`` 두 번째 등장 → 즉시 break (반복 무의미)
    - ``max_attempts`` 도달 → 마지막 시도 결과 반환 (caller 가 fallback 처리)
    """
    attempts: list[CodeAttempt] = []
    early_break_reason: str | None = None

    for attempt_no in range(1, max_attempts + 1):
        # 직전 시도들의 hint 를 ctx 에 주입해 새 ScoutContext 만들기
        # (ScoutContext 는 frozen 아니므로 model_copy 가능)
        attempt_ctx = scout_ctx.model_copy(
            update={"previous_attempts": [_hint_from_attempt(a) for a in attempts]}
        )

        scout_step_result = await run_scout_once(orch, attempt_ctx, observer)
        scout_out: ScoutOutput | None = scout_step_result.payload_as(ScoutOutput)
        if scout_out is None:
            await observer.emit(
                pj_event(
                    "pj.scout.code_attempt",
                    role="scout",
                    ok=False,
                    attempt=attempt_no,
                    error="scout_output_missing",
                )
            )
            early_break_reason = "scout_output_missing"
            break

        code_hash = hashlib.sha256(scout_out.screening_code.encode("utf-8")).hexdigest()

        # ProgressGuard 사상: 같은 코드 반복 → 더 시도해도 결과 같음
        if any(a.code_hash == code_hash for a in attempts):
            await observer.emit(
                pj_event(
                    "pj.scout.code_repeated",
                    role="scout",
                    ok=False,
                    attempt=attempt_no,
                    code_hash=code_hash[:16],
                )
            )
            early_break_reason = "code_repeated"
            # 같은 결과의 attempt 를 또 누적할 필요 없음 — 마지막 attempt 의 결과 그대로 사용
            break

        # sandbox 실행 (검증 = 실 실행)
        screening_result = await screening.run(scout_out.screening_code, screening_context)

        attempts.append(
            CodeAttempt(
                attempt_no=attempt_no,
                scout_out=scout_out,
                scout_step_result=scout_step_result,
                code_hash=code_hash,
                result=screening_result,
            )
        )

        await observer.emit(
            pj_event(
                "pj.scout.code_attempt",
                role="scout",
                ok=screening_result.ok and bool(screening_result.candidates),
                attempt=attempt_no,
                error=screening_result.error,
                candidate_count=len(screening_result.candidates),
                code_hash=code_hash[:16],
            )
        )

        if screening_result.ok and screening_result.candidates:
            # 성공
            return ValidatedScoutResult(
                scout_out=scout_out,
                scout_step_result=scout_step_result,
                result=screening_result,
                attempts=attempts,
                accepted=True,
                early_break_reason=None,
            )

    # 모든 시도 실패 또는 조기 break
    if not attempts:
        # scout_out 자체가 한 번도 안 나옴 — caller 가 처리
        empty_result = ScreeningResult(ok=False, error="subprocess_error", details="no attempts")
        # ScoutOutput 도 없음 — 빈 placeholder
        placeholder = ScoutOutput(
            screening_code="def screen(market_data, context):\n    return []\n",
            hypothesis="(scout output 누락)",
            expected_candidates=0,
            factor_weights={},
            strategy_tags_used=[],
        )
        await observer.emit(
            pj_event("pj.scout.escalate", role="scout", ok=False, attempts=0, reason="no_output")
        )
        return ValidatedScoutResult(
            scout_out=placeholder,
            scout_step_result=None,
            result=empty_result,
            attempts=[],
            accepted=False,
            early_break_reason=early_break_reason or "no_output",
        )

    last = attempts[-1]
    await observer.emit(
        pj_event(
            "pj.scout.escalate",
            role="scout",
            ok=False,
            attempts=len(attempts),
            last_error=last.result.error,
            early_break=early_break_reason,
        )
    )
    return ValidatedScoutResult(
        scout_out=last.scout_out,
        scout_step_result=last.scout_step_result,
        result=last.result,
        attempts=attempts,
        accepted=False,
        early_break_reason=early_break_reason,
    )
