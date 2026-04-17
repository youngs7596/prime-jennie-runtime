"""Screening Executor 결과 스키마.

executor.py가 stdout으로 직렬화해서 내보내는 dict 형식. ScreeningToolAdapter가
역직렬화해서 list[ScreeningCandidate]로 환원한다.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from prime_jennie_runtime.slow_loop.scout.schemas import ScreeningCandidate

# SCOUT §8.3 — error code enum
ErrorCode = Literal[
    "import_violation",
    "forbidden_call",
    "compile_error",
    "screen_not_defined",
    "runtime_error",
    "invalid_return_type",
    "timeout",
    "subprocess_error",
]


class ScreeningResult(BaseModel):
    """Screening Executor 단일 실행 결과.

    성공 시 ok=True, candidates 채움. 실패 시 ok=False, error/details 채움.
    """

    ok: bool
    candidates: list[ScreeningCandidate] = Field(default_factory=list)
    error: ErrorCode | None = None
    details: str | list[str] | None = None
    exec_time_ms: Annotated[int, Field(ge=0)] = 0
    truncated: bool = False  # 21+ candidates 반환 → 상위 20개로 cap된 경우 True
